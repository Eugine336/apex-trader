"""APEX TRADER — CandleCloseHandler (Phase 3).

Subscribes to ``CandleClose`` events from the Phase 2 EventBus and
incrementally updates WorldModels by running the relevant brain modules
for the closed timeframe.  Runs ALONGSIDE the timer-based ScanScheduler
(which remains the safety net until Phase 8 validates this path).

The handler keeps the WorldModel fresh between full scans:
  - M5 close  → FVG, OrderBlock, Volume, Inducement
  - M15 close → FVG
  - H1 close  → FVG, OrderBlock, Liquidity, Volume, Wyckoff, Structure
  - H4 close  → OrderBlock, Liquidity, Structure
  - D1 close  → Structure

Each (symbol, timeframe) analysis runs in a thread pool so the EventBus
callback returns immediately.  A skip-if-unchanged optimisation avoids
redundant computation when a candle close event fires for data that has
already been processed.
"""

from __future__ import annotations

import hashlib
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import pandas as pd
from loguru import logger

from brain.fvg_detector import FVGDetector, FairValueGap
from brain.inducement_detector import InducementDetector, InducementAnalysis
from brain.instrument_profile import get_profile
from brain.liquidity_mapper import LiquidityMapper, LiquidityMap
from brain.order_block import OrderBlockDetector, OrderBlock
from brain.structure_engine import StructureEngine, StructureAnalysis
from brain.volume_analyzer import VolumeAnalyzer, VolumeAnalysis
from brain.world_model import WorldModel, WorldModelStore, build_world_model
from brain.wyckoff_engine import WyckoffEngine, WyckoffAnalysis
from config import get_pip_size
from entry.models import EntryConfig
from entry.zone_watcher import extract_entry_zones
from tick.event_bus import EventBus
from tick.models import CandleClose


TF_MODULE_MAP: dict[str, list[str]] = {
    "M5": ["fvg", "order_block", "volume", "inducement"],
    "M15": ["fvg"],
    "H1": ["fvg", "order_block", "liquidity", "volume", "wyckoff", "structure"],
    "H4": ["order_block", "liquidity", "structure"],
    "D1": ["structure"],
}

CandleFetcher = Callable[[str, str, int], Optional[pd.DataFrame]]


def _bar_hash(df: pd.DataFrame) -> str:
    """Hash the last bar of a DataFrame for skip-if-unchanged detection."""
    if df is None or df.empty:
        return ""
    last = df.iloc[-1]
    raw = f"{last.get('open', 0):.6f}|{last.get('high', 0):.6f}|{last.get('low', 0):.6f}|{last.get('close', 0):.6f}|{last.get('volume', 0)}"
    return hashlib.md5(raw.encode()).hexdigest()


def _compute_bias(struct_by_tf: dict[str, StructureAnalysis]) -> dict[str, Any]:
    """Synthesize a directional bias dict from per-TF StructureAnalysis.

    Direction is decided by H4 + H1 (D1 only weights confidence), mirroring
    ``StructureEngine.get_bias`` but operating on the already-computed
    analyses stored in the WorldModel so no extra candle fetch is needed.
    Returns the schema consumers expect: ``direction`` ("LONG"/"SHORT"/""),
    ``score`` (0-100), ``opposing_boost``, plus per-TF trends.
    """
    def _trend(tf: str) -> tuple[Optional[str], float]:
        sa = struct_by_tf.get(tf)
        if sa is None:
            return None, 0.0
        trend = sa.trend.value if hasattr(sa.trend, "value") else str(sa.trend)
        return trend, float(getattr(sa, "confidence", 0.0) or 0.0)

    h4_t, h4_c = _trend("H4")
    h1_t, h1_c = _trend("H1")
    d1_t, d1_c = _trend("D1")

    direction_trend: Optional[str] = None
    strength = "NONE"
    if h4_t and h4_t != "RANGING" and h4_t == h1_t:
        direction_trend, strength = h4_t, "STRONG"
    elif h4_t and h4_t != "RANGING" and (h1_t is None or h1_t == "RANGING"):
        direction_trend, strength = h4_t, "MODERATE"
    elif (h4_t is None or h4_t == "RANGING") and h1_t and h1_t != "RANGING":
        direction_trend, strength = h1_t, "MODERATE"
    elif h4_t and h1_t and h4_t != h1_t:
        strength = "CONFLICTED"

    if direction_trend == "BULLISH":
        direction = "LONG"
    elif direction_trend == "BEARISH":
        direction = "SHORT"
    else:
        direction = ""

    d1_aligned = bool(
        direction_trend and d1_t and d1_t != "RANGING" and d1_t == direction_trend
    )
    if d1_t and d1_t != "RANGING":
        if d1_aligned:
            confidence = round(d1_c * 0.4 + h4_c * 0.35 + h1_c * 0.25, 2)
        else:
            confidence = round(((h4_c + h1_c) / 2) * 0.85, 2)
    else:
        confidence = round((h4_c + h1_c) / 2, 2)

    score = int(round(confidence * 100)) if direction else 0

    return {
        "direction": direction,
        "score": score,
        "opposing_boost": 0,
        "strength": strength,
        "h4_trend": h4_t or "UNKNOWN",
        "h1_trend": h1_t or "UNKNOWN",
        "d1_trend": d1_t or "UNKNOWN",
        "d1_aligned": d1_aligned,
        "confidence": confidence,
        "tradeable": strength in ("STRONG", "MODERATE"),
    }


class CandleCloseHandler:
    """Incrementally updates WorldModels on candle-close events.

    Subscribes to the EventBus ``"candle_close"`` topic.  When a
    ``CandleClose`` event arrives, fetches fresh candles, runs the
    relevant brain modules for that timeframe, and merges the results
    into the existing ``WorldModel`` for the symbol.

    Thread-safe: analysis runs in a bounded ``ThreadPoolExecutor``; the
    WorldModelStore handles concurrent publishes via its internal RLock.
    """

    def __init__(
        self,
        event_bus: EventBus,
        world_model_store: WorldModelStore,
        candle_fetcher: CandleFetcher,
        max_workers: int = 4,
        candle_count: int = 200,
        entry_config: Optional[EntryConfig] = None,
        edge_weight: Optional[Callable[[str, str], float]] = None,
    ) -> None:
        self._bus = event_bus
        self._store = world_model_store
        self._fetcher = candle_fetcher
        self._candle_count = candle_count
        self._entry_config = entry_config or EntryConfig()
        self._edge_weight = edge_weight
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="cc-handler",
        )

        self._lock = threading.Lock()
        self._last_bar_hash: dict[tuple[str, str], str] = {}
        self._events_received: int = 0
        self._events_processed: int = 0
        self._events_skipped: int = 0
        self._running = True

        self._structure = StructureEngine()
        self._liquidity = LiquidityMapper()
        self._volume = VolumeAnalyzer()

        self._bus.subscribe("candle_close", self._on_candle_close)

    def shutdown(self) -> None:
        """Stop accepting new events and shut down the worker pool."""
        self._running = False
        self._bus.unsubscribe("candle_close", self._on_candle_close)
        self._pool.shutdown(wait=False)

    # ------------------------------------------------------------------
    # EventBus callback
    # ------------------------------------------------------------------

    def _on_candle_close(self, event: CandleClose) -> None:
        with self._lock:
            self._events_received += 1
        if not self._running:
            return
        if event.timeframe not in TF_MODULE_MAP:
            return
        self._pool.submit(self._handle, event)

    # ------------------------------------------------------------------
    # Core handler (runs in worker thread)
    # ------------------------------------------------------------------

    def _handle(self, event: CandleClose) -> None:
        symbol = event.symbol
        tf = event.timeframe
        try:
            df = self._fetcher(symbol, tf, self._candle_count)
            if df is None or df.empty:
                logger.debug(
                    "[cc-handler] no candle data for {} {} — skipping", symbol, tf,
                )
                return

            bh = _bar_hash(df)
            with self._lock:
                prev = self._last_bar_hash.get((symbol, tf))
                if prev == bh:
                    self._events_skipped += 1
                    return
                self._last_bar_hash[(symbol, tf)] = bh

            results = self._run_modules(symbol, tf, df)
            if results:
                self._merge_and_publish(symbol, tf, results, event.close_time)

            with self._lock:
                self._events_processed += 1

        except Exception as exc:
            logger.warning(
                "[cc-handler] error processing {} {}: {} — {}",
                symbol, tf, type(exc).__name__, exc,
            )

    # ------------------------------------------------------------------
    # Brain module execution
    # ------------------------------------------------------------------

    def _run_modules(
        self, symbol: str, tf: str, df: pd.DataFrame,
    ) -> dict[str, Any]:
        modules = TF_MODULE_MAP.get(tf, [])
        if not modules:
            return {}

        try:
            pip_size = get_pip_size(symbol)
        except KeyError:
            pip_size = 0.0001
        profile = get_profile(symbol)
        results: dict[str, Any] = {}

        for mod in modules:
            try:
                if mod == "fvg":
                    det = FVGDetector(
                        pip_size=pip_size,
                        proximity_pips=profile.fvg_proximity_pips,
                        min_size_pips=profile.fvg_min_size_pips,
                    )
                    results["fvg"] = det.detect(df, timeframe=tf)
                elif mod == "order_block":
                    det = OrderBlockDetector(
                        pip_size=pip_size,
                        min_impulse_pips=profile.ob_min_impulse_pips,
                        buffer_pips=profile.ob_buffer_pips,
                    )
                    results["order_block"] = det.detect(df, timeframe=tf)
                elif mod == "liquidity":
                    results["liquidity"] = self._liquidity.map(df, pip_size)
                elif mod == "volume":
                    results["volume"] = self._volume.analyze(df)
                elif mod == "wyckoff":
                    if profile.wyckoff_enabled:
                        wyck = WyckoffEngine(pip_size=pip_size)
                        results["wyckoff"] = wyck.analyze(df)
                elif mod == "inducement":
                    det = InducementDetector(pip_size=pip_size)
                    results["inducement"] = det.analyze(df)
                elif mod == "structure":
                    results["structure"] = self._structure.analyze(df)
            except Exception as exc:
                logger.debug(
                    "[cc-handler] {} {} module '{}' failed: {}",
                    symbol, tf, mod, exc,
                )

        return results

    # ------------------------------------------------------------------
    # WorldModel merge
    # ------------------------------------------------------------------

    def _merge_and_publish(
        self,
        symbol: str,
        tf: str,
        results: dict[str, Any],
        close_time: datetime,
    ) -> None:
        existing = self._store.get(symbol)

        fvgs = dict(existing.fvgs) if existing else {}
        obs = dict(existing.order_blocks) if existing else {}
        struct = dict(existing.structure) if existing else {}
        liq = dict(existing.liquidity) if existing else {}
        vol = dict(existing.volume) if existing else {}
        wyck = dict(existing.wyckoff) if existing else {}
        ind = dict(existing.inducement) if existing else {}

        if "fvg" in results:
            fvgs[tf] = list(results["fvg"])
        if "order_block" in results:
            obs[tf] = list(results["order_block"])
        if "structure" in results:
            struct[tf] = results["structure"]
        if "liquidity" in results:
            liq[tf] = results["liquidity"]
        if "volume" in results:
            vol[tf] = results["volume"]
        if "wyckoff" in results:
            wyck[tf] = results["wyckoff"]
        if "inducement" in results:
            ind[tf] = results["inducement"]

        # Synthesize the directional bias from the merged HTF structure.
        bias = _compute_bias(struct)

        wm = build_world_model(
            symbol=symbol,
            version=self._store.next_version(),
            timestamp=close_time,
            fvgs=fvgs,
            order_blocks=obs,
            structure=struct,
            liquidity=liq,
            volume=vol,
            wyckoff=wyck,
            inducement=ind,
            bias=bias,
        )

        # Synthesize the actionable entry layer so the WorldModel is the
        # single source of truth for the entry plane — consumers read
        # ``wm.entry_zones`` instead of re-deriving zones.  ``edge_weight``
        # makes the conviction data-driven (learned per-market edge).
        zones = extract_entry_zones(wm, self._entry_config, self._edge_weight)
        if zones:
            wm = replace(wm, entry_zones=tuple(zones))

        self._store.publish(wm)
        self._bus.publish("world_model_update", symbol)
        logger.debug(
            "[cc-handler] published WorldModel for {} (tf={}, v={}, zones={})",
            symbol, tf, wm.version, len(wm.entry_zones),
        )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def events_received(self) -> int:
        with self._lock:
            return self._events_received

    @property
    def events_processed(self) -> int:
        with self._lock:
            return self._events_processed

    @property
    def events_skipped(self) -> int:
        with self._lock:
            return self._events_skipped

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "received": self._events_received,
                "processed": self._events_processed,
                "skipped": self._events_skipped,
            }

"""APEX TRADER — CandleCloseHandler (Phase 3).

Subscribes to ``CandleClose`` events from the Phase 2 EventBus and
incrementally updates WorldModels by running the relevant brain modules
for the closed timeframe.  This is the sole analysis path that keeps every
WorldModel fresh as bars close:
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
from brain.currency_strength import CurrencyStrengthMeter, CURRENCY_PAIRS
from brain.concept_modules import run_concepts
from config import get_pip_size
from entry.models import EntryConfig
from entry.zone_watcher import extract_entry_zones
from brain.decision_core import (
    TF_MODULE_MAP,
    compute_bias,
    run_tf_modules,
    blend_concepts,
    build_consensus,
    fvg_proximity,
)
from tick.event_bus import EventBus
from tick.models import CandleClose


CandleFetcher = Callable[[str, str, int], Optional[pd.DataFrame]]


def _bar_hash(df: pd.DataFrame) -> str:
    """Hash the last bar of a DataFrame for skip-if-unchanged detection."""
    if df is None or df.empty:
        return ""
    last = df.iloc[-1]
    raw = f"{last.get('open', 0):.6f}|{last.get('high', 0):.6f}|{last.get('low', 0):.6f}|{last.get('close', 0):.6f}|{last.get('volume', 0)}"
    return hashlib.md5(raw.encode()).hexdigest()


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
        edge_weight: Optional[Callable[..., float]] = None,
        concept_weight: Optional[Callable[..., float]] = None,
        vote_calibrator: Optional[Any] = None,
        module_governor: Optional[Any] = None,
        win_rate_provider: Optional[Any] = None,
    ) -> None:
        self._bus = event_bus
        self._store = world_model_store
        self._fetcher = candle_fetcher
        self._candle_count = candle_count
        self._entry_config = entry_config or EntryConfig()
        # Learned, bounded multipliers (default-neutral when None).  They make
        # the analysis combination data-driven: ``edge_weight`` scales zone
        # conviction by realized edge; ``concept_weight`` scales each non-ICT
        # concept's contribution to the bias blend.
        self._edge_weight = edge_weight
        self._concept_weight = concept_weight
        # Adaptive vote-panel hooks (default-neutral when None): the
        # VoteCalibrator scales each module's static consensus weight by its
        # learned accuracy multiplier, and the ModuleGovernor excludes
        # SHADOWED/DISABLED modules from the panel.  Both are no-ops unless the
        # operator turns their feature flag on.
        self._vote_calibrator = vote_calibrator
        self._module_governor = module_governor
        # Opportunity-ranker win-rate source (default-neutral when None): feeds
        # the ranker a calibrated per-pair win probability so candidate EV is
        # learned, not the hardcoded prior.
        self._win_rate_provider = win_rate_provider
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="cc-handler",
        )

        self._lock = threading.Lock()
        # Serializes the read-merge-version-publish sequence in
        # ``_merge_and_publish``.  Candle-close events are dispatched on a
        # worker pool, so two timeframes of the same symbol can merge+publish
        # concurrently; without this a stale read drops one timeframe's
        # contribution (lost update).  Separate from ``self._lock`` (which
        # guards counters/bar-hashes and is re-taken inside ``_build_consensus``)
        # to avoid self-deadlock.
        self._publish_lock = threading.RLock()
        self._last_bar_hash: dict[tuple[str, str], str] = {}
        self._events_received: int = 0
        self._events_processed: int = 0
        self._events_skipped: int = 0
        self._running = True

        self._structure = StructureEngine()
        self._liquidity = LiquidityMapper()
        self._volume = VolumeAnalyzer()
        self._strength_meter = CurrencyStrengthMeter()
        self._strength_cache: Optional[Any] = None
        self._strength_cache_ts: float = 0.0

        self._bus.subscribe("candle_close", self._on_candle_close)

    def shutdown(self) -> None:
        """Stop accepting new events and shut down the worker pool."""
        self._running = False
        self._bus.unsubscribe("candle_close", self._on_candle_close)
        self._pool.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Startup warmup (backfill)
    # ------------------------------------------------------------------

    def warmup(
        self,
        symbols: list[str],
        timeframes: Optional[list[str]] = None,
        per_symbol_timeout: float = 30.0,
    ) -> int:
        """Backfill WorldModels at startup so analysis isn't blind on cold start.

        Tick-driven analysis only refreshes a symbol's WorldModel once a candle
        closes *after* startup — leaving a window where higher timeframes
        (H1/H4/D1) have no structure/bias, and starving symbols that receive few
        ticks.  This proactively fetches recent candles for each symbol ×
        timeframe and runs the same brain-module pipeline a candle close would,
        populating the store (and the entry plane via ``world_model_update``)
        immediately.

        Each symbol's timeframes are processed sequentially so the per-symbol
        merge never races itself; different symbols run in parallel on the
        worker pool.  Must be called BEFORE the live loops start so it cannot
        race a live candle-close for the same symbol.  Best-effort: any fetch or
        analysis failure is logged and skipped.

        Returns the number of (symbol, timeframe) WorldModel updates published.
        """
        tfs = [tf for tf in (timeframes or list(TF_MODULE_MAP.keys()))
               if tf in TF_MODULE_MAP]
        if not symbols or not tfs:
            return 0

        futures = {
            self._pool.submit(self._warmup_symbol, sym, tfs): sym
            for sym in symbols
        }
        published = 0
        for fut, sym in futures.items():
            try:
                published += fut.result(timeout=per_symbol_timeout)
            except Exception as exc:
                logger.debug("[cc-handler] warmup failed for {}: {}", sym, exc)

        logger.info(
            "[cc-handler] warmup complete — {} WorldModel update(s) across "
            "{} symbol(s) × {} timeframe(s)",
            published, len(symbols), len(tfs),
        )
        return published

    def _warmup_symbol(self, symbol: str, timeframes: list[str]) -> int:
        n = 0
        for tf in timeframes:
            try:
                if self._warmup_one(symbol, tf):
                    n += 1
            except Exception as exc:
                logger.debug(
                    "[cc-handler] warmup {} {} failed: {}", symbol, tf, exc,
                )
        return n

    def _warmup_one(self, symbol: str, tf: str) -> bool:
        df = self._fetcher(symbol, tf, self._candle_count)
        if df is None or df.empty:
            return False
        # Seed the bar hash so the first live candle-close for the same
        # (unchanged) bar is correctly skipped as a duplicate.
        with self._lock:
            self._last_bar_hash[(symbol, tf)] = _bar_hash(df)
        results = self._run_modules(symbol, tf, df)
        if not results:
            return False
        current_price = 0.0
        try:
            current_price = float(df["close"].iloc[-1])
        except Exception:
            current_price = 0.0
        self._merge_and_publish(
            symbol, tf, results, datetime.now(timezone.utc), current_price,
        )
        return True

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
                current_price = 0.0
                try:
                    current_price = float(df["close"].iloc[-1])
                except Exception:
                    current_price = 0.0
                self._merge_and_publish(
                    symbol, tf, results, event.close_time, current_price,
                )

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
        """Delegate to the shared decision core (reusing this handler's engines)."""
        return run_tf_modules(
            symbol, tf, df,
            structure=self._structure,
            liquidity=self._liquidity,
            volume=self._volume,
        )

    # ------------------------------------------------------------------
    # WorldModel merge
    # ------------------------------------------------------------------

    def _merge_and_publish(
        self,
        symbol: str,
        tf: str,
        results: dict[str, Any],
        close_time: datetime,
        current_price: float = 0.0,
    ) -> None:
        # Hold the publish lock across the whole read-merge-version-publish
        # sequence so concurrent worker-pool handlers cannot drop a timeframe
        # via a stale read (see ``self._publish_lock``).
        with self._publish_lock:
            self._merge_and_publish_locked(
                symbol, tf, results, close_time, current_price,
            )

    def _merge_and_publish_locked(
        self,
        symbol: str,
        tf: str,
        results: dict[str, Any],
        close_time: datetime,
        current_price: float = 0.0,
    ) -> None:
        existing = self._store.get(symbol)

        fvgs = dict(existing.fvgs) if existing else {}
        obs = dict(existing.order_blocks) if existing else {}
        struct = dict(existing.structure) if existing else {}
        liq = dict(existing.liquidity) if existing else {}
        vol = dict(existing.volume) if existing else {}
        wyck = dict(existing.wyckoff) if existing else {}
        ind = dict(existing.inducement) if existing else {}
        concepts = dict(existing.concepts) if existing else {}
        regime = dict(existing.regime) if existing else {}

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
        if "concepts" in results:
            concepts[tf] = list(results["concepts"])
        if "regime" in results:
            regime[tf] = results["regime"]

        # Synthesize the directional bias from the merged HTF structure, then
        # blend in the non-ICT concepts (weighted by their learned edge) so the
        # bias is a data-driven combination, not pure ICT structure.
        bias = compute_bias(struct)
        bias = self._blend_concepts(bias, concepts, regime)

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
            concepts=concepts,
            regime=regime,
        )

        # Synthesize the actionable entry layer so the WorldModel is the
        # single source of truth for the entry plane — consumers read
        # ``wm.entry_zones`` instead of re-deriving zones.  The learned
        # ``edge_weight`` scales each zone's conviction by realized edge.
        zones = extract_entry_zones(wm, self._entry_config, self._edge_weight)
        if zones:
            wm = replace(wm, entry_zones=tuple(zones))

        # Synthesize per-module directional votes + ranked opportunity
        # candidates so the dashboard's module-votes and ranker panels read
        # them straight off the WorldModel.  Best-effort and read-only — a
        # failure here must never block the publish or the trading path.
        try:
            votes, candidates = self._build_consensus(symbol, wm, current_price)
            if votes or candidates:
                wm = replace(wm, votes=tuple(votes), candidates=tuple(candidates))
        except Exception as exc:
            logger.debug("[cc-handler] {} consensus build failed: {}", symbol, exc)

        self._store.publish(wm)
        self._bus.publish("world_model_update", symbol)
        logger.debug(
            "[cc-handler] published WorldModel for {} (tf={}, v={}, zones={})",
            symbol, tf, wm.version, len(wm.entry_zones),
        )

    def _build_consensus(
        self, symbol: str, wm: WorldModel, current_price: float,
    ) -> tuple[list, list]:
        """Derive per-module directional votes + ranked opportunities.

        Reuses the same vote extractors and opportunity ranker the legacy
        scanner used, sourced from the WorldModel's already-computed
        analysis plus optional raw-series voters (momentum, VWAP, currency
        strength, liquidity sweep).  Every auxiliary fetch is best-effort.
        """
        m5_df = None
        h1_df = None
        session_open_minutes = 0
        try:
            m5_df = self._fetcher(symbol, "M5", self._candle_count)
        except Exception as exc:
            logger.debug("[cc-handler] {} M5 fetch for consensus failed: {}", symbol, exc)
        try:
            h1_df = self._fetcher(symbol, "H1", self._candle_count)
        except Exception as exc:
            logger.debug("[cc-handler] {} H1 fetch for consensus failed: {}", symbol, exc)

        if m5_df is not None and len(m5_df) > 1:
            try:
                if "time" in m5_df.columns:
                    t0 = pd.to_datetime(m5_df["time"].iloc[0], utc=True, errors="coerce")
                    t1 = pd.to_datetime(m5_df["time"].iloc[-1], utc=True, errors="coerce")
                    if pd.notna(t0) and pd.notna(t1):
                        session_open_minutes = max(
                            0, int((t1 - t0).total_seconds() / 60.0),
                        )
                if session_open_minutes <= 0:
                    session_open_minutes = max(0, int((len(m5_df) - 1) * 5))
            except Exception as exc:
                logger.debug("[cc-handler] {} session-minutes derive failed: {}", symbol, exc)
                session_open_minutes = 0

        cs_analysis = None
        try:
            prof = get_profile(symbol)
            if getattr(prof, "currency_strength_enabled", False):
                cs_analysis = self._currency_strength_analysis()
        except Exception as exc:
            logger.debug("[cc-handler] {} currency-strength prep failed: {}", symbol, exc)

        return build_consensus(
            symbol,
            wm,
            current_price,
            m5_df=m5_df,
            h1_df=h1_df,
            liquidity_mapper=self._liquidity,
            session_open_minutes=session_open_minutes,
            currency_strength_analysis=cs_analysis,
            currency_pairs=CURRENCY_PAIRS,
            vote_calibrator=self._vote_calibrator,
            module_governor=self._module_governor,
            win_rate_provider=self._win_rate_provider,
        )

    def _currency_strength_analysis(self) -> Optional[Any]:
        """Best-effort cached currency-strength analysis for consensus voting."""
        try:
            now = _time.monotonic()
            with self._lock:
                if (
                    self._strength_cache is not None
                    and (now - self._strength_cache_ts) < 300.0
                ):
                    return self._strength_cache
            price_data: dict[str, pd.DataFrame] = {}
            for pair in CURRENCY_PAIRS:
                try:
                    df = self._fetcher(pair, "M5", self._candle_count)
                    if df is not None and len(df) >= 30:
                        price_data[pair] = df
                except Exception as exc:
                    logger.debug("[cc-handler] currency-strength fetch {} failed: {}", pair, exc)
            if not price_data:
                return None
            analysis = self._strength_meter.calculate(price_data)
            with self._lock:
                self._strength_cache = analysis
                self._strength_cache_ts = now
            return analysis
        except Exception as exc:
            logger.debug("[cc-handler] currency-strength analysis failed: {}", exc)
            return None

    def _blend_concepts(
        self,
        bias: dict[str, Any],
        concepts_by_tf: dict[str, list],
        regime_by_tf: dict[str, str],
    ) -> dict[str, Any]:
        """Blend non-ICT concept signals into the structural bias.

        Each directional concept votes (LONG/SHORT) weighted by its strength
        and learned per-concept edge.  The net vote nudges the bias ``score``
        within a bounded range and is recorded for observability — it never
        flips the structural ``direction`` or ``tradeable`` decision, so the
        ICT plane stays authoritative and the blend is default-neutral when
        concepts are neutral or unproven.
        """
        return blend_concepts(bias, concepts_by_tf, regime_by_tf, self._concept_weight)

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

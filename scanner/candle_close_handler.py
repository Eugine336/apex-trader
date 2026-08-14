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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import pandas as pd
from loguru import logger

from brain.liquidity_mapper import LiquidityMapper
from brain.structure_engine import StructureEngine
from brain.volume_analyzer import VolumeAnalyzer
from brain.world_model import WorldModelStore, build_world_model
from entry.models import EntryConfig
from brain.decision_core import (
    TF_MODULE_MAP,
    run_tf_modules,
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
        ranker_config: Optional[Any] = None,
        calibration_engine: Optional[Any] = None,
        get_spread_pips: Optional[Callable[[str], float]] = None,
        calibration_spread_tf: str = "M5",
        news_impact_tracker: Optional[Any] = None,
        developing_store: Optional[WorldModelStore] = None,
        compression_detector: Optional[Any] = None,
    ) -> None:
        self._bus = event_bus
        self._store = world_model_store
        self._fetcher = candle_fetcher
        self._candle_count = candle_count
        self._entry_config = entry_config or EntryConfig()
        # Learned, bounded multiplier (default-neutral when None): ``edge_weight``
        # scales each entry zone's conviction by its realized edge.
        self._edge_weight = edge_weight
        # CalibrationEngine feed (default-neutral when None): on each candle
        # close the handler hands the SAME candles/spread it already fetched to
        # the single-writer CalibrationEngine so per-symbol stats stay live.
        self._calibration_engine = calibration_engine
        self._get_spread_pips = get_spread_pips
        self._calibration_spread_tf = calibration_spread_tf
        # News-impact measurement trigger (default-neutral when None): on each
        # configured-TF close it captures price at high-impact news events and
        # measures the realised reaction ~30 min later, feeding the
        # CalibrationEngine's learned news sensitivity.
        self._news_impact_tracker = news_impact_tracker
        # Global compression / market-state detector (default-neutral when
        # None). Fed the SAME candles this handler already fetches on each
        # tracked (M5/M15) close so its BBW-percentile classification stays
        # live. Read elsewhere (entry orchestrator logs it); it never blocks
        # analysis here.
        self._compression_detector = compression_detector
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
        # guards counters/bar-hashes) to avoid self-deadlock.
        self._publish_lock = threading.RLock()
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
        self._maybe_update_compression(symbol, tf, df)
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
            # Global compression / market-state update — piggybacks on the same
            # candles already fetched for the tracked (M5/M15) timeframes.
            self._maybe_update_compression(symbol, tf, df)
            # Spread sample — fed once per configured TF close (live only; the
            # getter reads the cached broker tick) so spread median/p95 calibrate.
            if (
                self._calibration_engine is not None
                and self._get_spread_pips is not None
                and tf == self._calibration_spread_tf
            ):
                try:
                    self._calibration_engine.update_spread(
                        symbol, float(self._get_spread_pips(symbol) or 0.0),
                    )
                except Exception as exc:
                    logger.debug("[cc-handler] calibration spread feed failed: {}", exc)
            # News-impact measurement — piggybacks on the same configured-TF
            # close (M5 by default). Captures price at fired high-impact events
            # and measures the reaction once matured, feeding learned news
            # sensitivity. Best-effort; never breaks the feed.
            if (
                self._news_impact_tracker is not None
                and tf == self._calibration_spread_tf
            ):
                try:
                    self._news_impact_tracker.on_m5_close(
                        symbol, float(df["close"].iloc[-1]), now=event.close_time,
                    )
                except Exception as exc:
                    logger.debug("[cc-handler] news-impact feed failed: {}", exc)
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
        # CalibrationEngine feed — the single writer ingests the freshly-fetched
        # candles to keep per-symbol ATR / session stats live (covers warmup too).
        if self._calibration_engine is not None:
            try:
                from config import get_pip_size
                try:
                    pip = get_pip_size(symbol)
                except Exception:
                    pip = 0.0001
                self._calibration_engine.update_candles(symbol, tf, df, pip)
            except Exception as exc:
                logger.debug("[cc-handler] calibration candle feed failed: {}", exc)
        return run_tf_modules(
            symbol, tf, df,
            structure=self._structure,
            liquidity=self._liquidity,
            volume=self._volume,
        )

    def _maybe_update_compression(
        self, symbol: str, tf: str, df: pd.DataFrame,
    ) -> None:
        """Feed the global compression detector on a tracked-TF close.

        No-op when no detector is wired. The detector itself ignores untracked
        timeframes, so this is safe to call on every close. Best-effort — a
        compression failure must never break the analysis path.
        """
        if self._compression_detector is None:
            return
        try:
            self._compression_detector.update(symbol, tf, df)
        except Exception as exc:  # noqa: BLE001 — detector must never block analysis
            logger.debug(
                "[cc-handler] compression update failed for {} {}: {}",
                symbol, tf, exc,
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

        # Constitutional cutover (Part XXV): the live WorldModel carries only
        # non-directional FACTS (structure geometry, FVG/OB/liquidity/volume/
        # concepts/regime).  A synthesized directional ``bias`` no longer rides
        # on the world model — the Cognitive Brain is the sole authority that
        # reads the raw state and forms direction.
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
            concepts=concepts,
            regime=regime,
        )

        self._store.publish(wm)
        self._bus.publish("world_model_update", symbol)
        logger.debug(
            "[cc-handler] published WorldModel for {} (tf={}, v={})",
            symbol, tf, wm.version,
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

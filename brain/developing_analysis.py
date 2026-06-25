"""APEX TRADER — DevelopingAnalysisLoop (Phase 2 of Institutional Market Model).

A background daemon thread that continuously analyzes the *forming* candles
built by the Phase-1 :class:`tick.live_candle_aggregator.LiveCandleAggregator`,
producing a **developing** WorldModel that evolves between candle closes.

Why this exists
---------------
The confirmed analysis path (``CandleCloseHandler`` → broker fetch on candle
close) only refreshes a timeframe's structure when its candle closes — up to an
hour stale on H1, four hours on H4, a day on D1.  Professional traders maintain
a live, evolving read of the developing higher-timeframe candle.  This loop
gives the system that live read **without** corrupting the confirmed,
non-repainting structure: developing analysis is published to a **separate**
:class:`~brain.world_model.WorldModelStore`, and the confirmed path only ever
reads it to nudge bias *confidence* (never direction).

Design guarantees
-----------------
* **Additive** — the confirmed path is untouched in behaviour.
* **Best-effort** — every analysis is wrapped; a failure is logged, never
  crashes the loop or the system.
* **Thread-safe** — its own ``StructureEngine``/``LiquidityMapper``/
  ``VolumeAnalyzer`` instances (not shared with ``CandleCloseHandler``), a
  publish lock serializing the read-merge-publish per symbol, and a separate
  thread pool that never competes with the confirmed analysis pool.
* **Zero broker fetches** — reads forming candles straight from the in-memory
  aggregator.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from brain.decision_core import TF_MODULE_MAP, compute_bias, run_tf_modules
from brain.liquidity_mapper import LiquidityMapper
from brain.structure_engine import StructureEngine
from brain.volume_analyzer import VolumeAnalyzer
from brain.world_model import WorldModelStore, build_world_model

if TYPE_CHECKING:  # avoid import coupling on the hot tick module
    from config import DevelopingAnalysisConfig
    from tick.live_candle_aggregator import LiveCandleAggregator


class DevelopingAnalysisLoop:
    """Background thread that continuously analyzes forming candles.

    Reads from a :class:`LiveCandleAggregator`, runs the brain modules WITH the
    forming bar included, and publishes developing WorldModels to a separate
    store.  Each ``(symbol, timeframe)`` is rate-limited to its configured
    refresh interval so the loop's CPU cost stays bounded for 65 instruments.
    """

    def __init__(
        self,
        candle_aggregator: "LiveCandleAggregator",
        developing_store: WorldModelStore,
        symbols: list[str],
        config: "DevelopingAnalysisConfig",
    ) -> None:
        self._aggregator = candle_aggregator
        self._developing_store = developing_store
        self._symbols = list(symbols)
        self._config = config

        # Own engine instances — never shared with the confirmed
        # CandleCloseHandler, so there is no cross-thread state to coordinate.
        self._structure = StructureEngine()
        self._liquidity = LiquidityMapper()
        self._volume = VolumeAnalyzer()

        # Dedicated pool — separate from the confirmed analysis pool so
        # developing work can never starve candle-close processing.
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(getattr(config, "max_workers", 4))),
            thread_name_prefix="dev-analysis",
        )

        # Per-(symbol, tf) monotonic timestamp of last analysis for rate limit.
        self._last_analysis: dict[tuple[str, str], float] = {}
        # Serializes the read-merge-publish per symbol (mirrors the confirmed
        # handler's publish lock so two TFs of one symbol can't drop each
        # other's contribution via a stale read).
        self._publish_lock = threading.RLock()

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Per-TF refresh interval (seconds). M1 is intentionally absent — it is
        # not in TF_MODULE_MAP (M1 is the entry-confirmation plane only).
        self._refresh: dict[str, float] = {
            "M5": float(getattr(config, "refresh_m5", 10.0)),
            "M15": float(getattr(config, "refresh_m15", 15.0)),
            "H1": float(getattr(config, "refresh_h1", 30.0)),
            "H4": float(getattr(config, "refresh_h4", 60.0)),
            "D1": float(getattr(config, "refresh_d1", 300.0)),
        }

        # Observability counters.
        self._cycles: int = 0
        self._analyses_run: int = 0
        self._analyses_failed: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="dev-analysis-loop",
        )
        self._thread.start()
        logger.info(
            "[developing] started — {} symbol(s), refresh(s)={}",
            len(self._symbols), self._refresh,
        )

    def stop(self) -> None:
        self._running = False
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        try:
            self._pool.shutdown(wait=False)
        except Exception:  # pragma: no cover - defensive
            pass
        logger.info(
            "[developing] stopped (cycles={}, analyses_run={}, failed={})",
            self._cycles, self._analyses_run, self._analyses_failed,
        )

    def stats(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "cycles": self._cycles,
            "analyses_run": self._analyses_run,
            "analyses_failed": self._analyses_failed,
            "symbols_tracked": len(self._symbols),
            "developing_models": len(self._developing_store),
        }

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main loop — scans all symbols × TFs and submits due analyses."""
        while self._running:
            try:
                self._cycle()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "[developing] cycle error: {} — {}", type(exc).__name__, exc,
                )
            time.sleep(1.0)

    def _cycle(self) -> None:
        self._cycles += 1
        now = time.monotonic()
        for tf in TF_MODULE_MAP:
            refresh = self._refresh.get(tf)
            if refresh is None:
                continue  # TF not scheduled for developing analysis
            for symbol in self._symbols:
                key = (symbol, tf)
                last = self._last_analysis.get(key, 0.0)
                if (now - last) < refresh:
                    continue
                self._last_analysis[key] = now
                try:
                    self._pool.submit(self._analyze_one, symbol, tf)
                except RuntimeError:
                    # Pool shutting down — stop submitting.
                    return

    def _analyze_one(self, symbol: str, tf: str) -> None:
        try:
            df = self._aggregator.get_dataframe(symbol, tf)
            if df is None or df.empty:
                return
            results = run_tf_modules(
                symbol, tf, df,
                structure=self._structure,
                liquidity=self._liquidity,
                volume=self._volume,
                include_forming=True,  # KEY: keep the forming bar
            )
            if not results:
                return
            self._merge_and_publish(symbol, tf, results)
            self._analyses_run += 1
        except Exception as exc:
            self._analyses_failed += 1
            logger.debug(
                "[developing] {} {} analysis failed: {} — {}",
                symbol, tf, type(exc).__name__, exc,
            )

    def _merge_and_publish(self, symbol: str, tf: str, results: dict) -> None:
        """Merge one TF's developing results into the developing WorldModel.

        Simpler than the confirmed handler: no entry zones, no consensus votes,
        no quality layer — developing analysis only feeds bias confidence.
        """
        with self._publish_lock:
            existing = self._developing_store.get(symbol)

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

            # Developing bias from developing structure only (no confirmed
            # blend here — the blend happens in the confirmed path's
            # compute_bias, which reads this store).
            bias = compute_bias(struct)

            wm = build_world_model(
                symbol=symbol,
                version=self._developing_store.next_version(),
                timestamp=datetime.now(timezone.utc),
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
            self._developing_store.publish(wm)

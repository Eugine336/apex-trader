"""APEX TRADER — Adaptive Scheduler Loop (Phase 6, Part A).

A background daemon thread that drives the time-based learning cadences that
previously never fired in production:

* ``TunerAgent.on_periodic_tick`` — wakes the PERIODIC tunables (GateTuner EV
  threshold, planner calibrator, etc.) whose writers were dormant because only
  ``on_trade_close`` was wired live.
* ``TunerAgent.on_scan_cycle`` — wakes PER_SCAN_CYCLE tunables.
* ``AdaptiveWeightProvider.recompute`` — nudges the probabilistic-bias evidence
  weights toward the better-performing timeframes (bounded).

It is the missing scheduler that closes the continuous-learning loop. Mirrors
the :class:`brain.developing_analysis.DevelopingAnalysisLoop` daemon pattern:
1-second tick, monotonic-clock cadence gating, fully exception-safe, never
blocks or crashes the trading loop.

Leaf-ish — stdlib + loguru + adaptive.tunable only.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from loguru import logger

from adaptive.tunable import TuneContext


class AdaptiveSchedulerLoop:
    """Drives periodic / per-scan tuning and weight recompute on a timer."""

    def __init__(
        self,
        *,
        tuner_agent: Optional[Any],
        weight_provider: Optional[Any] = None,
        trade_count_provider: Optional[Callable[[], int]] = None,
        periodic_interval_seconds: float = 900.0,
        scan_interval_seconds: float = 3600.0,
        enabled: bool = True,
    ) -> None:
        self._agent = tuner_agent
        self._weights = weight_provider
        self._trade_count = trade_count_provider or (lambda: 0)
        self._periodic_interval = max(1.0, float(periodic_interval_seconds))
        self._scan_interval = max(1.0, float(scan_interval_seconds))
        self.enabled = bool(enabled)

        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Seed last-run so the first tick does not immediately fire everything
        # at once on startup; let the system warm up for one interval.
        now = time.monotonic()
        self._last_periodic = now
        self._last_scan = now

        self._periodic_runs = 0
        self._scan_runs = 0
        self._weight_recomputes = 0

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running or not self.enabled:
            if not self.enabled:
                logger.info("[adaptive-scheduler] disabled by config — not starting")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="adaptive-scheduler",
        )
        self._thread.start()
        logger.info(
            "[adaptive-scheduler] started — periodic={}s scan={}s",
            self._periodic_interval, self._scan_interval,
        )

    def stop(self) -> None:
        self._running = False
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        logger.info(
            "[adaptive-scheduler] stopped (periodic={}, scan={}, recomputes={})",
            self._periodic_runs, self._scan_runs, self._weight_recomputes,
        )

    def stats(self) -> dict:
        return {
            "running": self._running,
            "enabled": self.enabled,
            "periodic_interval_seconds": self._periodic_interval,
            "scan_interval_seconds": self._scan_interval,
            "periodic_runs": self._periodic_runs,
            "scan_runs": self._scan_runs,
            "weight_recomputes": self._weight_recomputes,
        }

    # ── Loop ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("[adaptive-scheduler] tick error: {}", exc)
            time.sleep(1.0)

    def _tick(self) -> None:
        now = time.monotonic()
        if (now - self._last_periodic) >= self._periodic_interval:
            self._last_periodic = now
            self._run_periodic()
        if (now - self._last_scan) >= self._scan_interval:
            self._last_scan = now
            self._run_scan()

    def _ctx(self) -> TuneContext:
        try:
            total = int(self._trade_count())
        except Exception:  # noqa: BLE001
            total = 0
        return TuneContext(
            total_trades=total,
            trades_since_last_tune=0,
            seconds_since_last_tune=0.0,
        )

    def _run_periodic(self) -> None:
        # Drive the PERIODIC tunables (formerly dormant writers).
        if self._agent is not None:
            try:
                self._agent.on_periodic_tick(self._ctx())
                self._periodic_runs += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("[adaptive-scheduler] on_periodic_tick failed: {}", exc)
        # Recompute the adaptive evidence weights on the same cadence.
        if self._weights is not None:
            try:
                if self._weights.recompute():
                    self._weight_recomputes += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("[adaptive-scheduler] weight recompute failed: {}", exc)

    def _run_scan(self) -> None:
        if self._agent is not None:
            try:
                self._agent.on_scan_cycle(self._ctx())
                self._scan_runs += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("[adaptive-scheduler] on_scan_cycle failed: {}", exc)


__all__ = ["AdaptiveSchedulerLoop"]

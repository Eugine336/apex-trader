"""Tick-cycle latency profiler for APEX TRADER (P4).

A lightweight, stdlib-only instrument that measures how long each component of
the trading tick takes, so an operator can see *where* the cycle spends its
time and decide what to move off the hot path.

Design goals:

* **Zero-cost when disabled** — :meth:`TickProfiler.measure` returns a shared
  no-op context manager when ``enabled`` is ``False``; no timing, no recording,
  no allocation per call.
* **Bounded memory** — each component keeps at most ``window_size`` recent
  durations in a ``deque``; the buffer never grows unbounded.
* **Concurrency-safe** — all mutations are guarded by a lock so the profiler is
  safe even if a component is timed from a worker thread.
* **Fail-safe** — a profiler fault must never disturb the tick; ``measure``
  always records best-effort and never re-raises its own bookkeeping errors.

It complements (does not replace) the existing ``_cycle_durations_ms`` ring
buffer in the trading loop: that tracks *total* cycle time for the Performance
panel, while this attributes time to *individual components* within the tick.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Deque, Dict, Iterator, List


def _percentile(ordered: List[float], q: float) -> float:
    """Nearest-rank percentile of an already-sorted list. ``q`` in [0, 1]."""
    if not ordered:
        return 0.0
    if q <= 0:
        return ordered[0]
    if q >= 1:
        return ordered[-1]
    idx = int(round(q * (len(ordered) - 1)))
    idx = max(0, min(len(ordered) - 1, idx))
    return ordered[idx]


class _NullTimer:
    """A reusable no-op context manager for the disabled fast path."""

    __slots__ = ()

    def __enter__(self) -> "_NullTimer":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


_NULL_TIMER = _NullTimer()


class TickProfiler:
    """Per-component timing of the trading tick cycle.

    Usage::

        with profiler.measure("regime_detector"):
            regime_detector.update(...)
        ...
        profiler.record_tick(total_ms)   # once per completed tick
    """

    def __init__(
        self,
        enabled: bool = True,
        slow_tick_threshold_ms: float = 100.0,
        window_size: int = 1000,
    ) -> None:
        self.enabled = bool(enabled)
        self.slow_tick_threshold_ms = float(slow_tick_threshold_ms)
        self.window_size = max(1, int(window_size))
        self._lock = threading.Lock()
        # component -> bounded deque of durations in ms
        self._component_ms: Dict[str, Deque[float]] = {}
        # cumulative call counts survive window eviction
        self._component_calls: Dict[str, int] = {}
        # total tick durations (ms)
        self._tick_ms: Deque[float] = deque(maxlen=self.window_size)
        # recent slow ticks: list of {"ms", "ts", "components"}
        self._slow_ticks: Deque[dict] = deque(maxlen=50)
        self._tick_count: int = 0
        self._slow_tick_count: int = 0
        # per-tick scratch: component -> ms accumulated within the current tick
        self._current_tick: Dict[str, float] = {}

    # ── Measurement ──────────────────────────────────────────────────────
    @contextmanager
    def _timed(self, component_name: str) -> Iterator[None]:
        start = time.perf_counter_ns()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000.0
            self._record(component_name, elapsed_ms)

    def measure(self, component_name: str):
        """Time a component within a tick.

        Returns a no-op context manager when disabled so the call site pays
        essentially nothing. Never raises.
        """
        if not self.enabled:
            return _NULL_TIMER
        return self._timed(component_name)

    def _record(self, component_name: str, elapsed_ms: float) -> None:
        """Record one component duration. Best-effort; never raises."""
        try:
            with self._lock:
                buf = self._component_ms.get(component_name)
                if buf is None:
                    buf = deque(maxlen=self.window_size)
                    self._component_ms[component_name] = buf
                buf.append(elapsed_ms)
                self._component_calls[component_name] = (
                    self._component_calls.get(component_name, 0) + 1
                )
                # accumulate into the in-flight tick (components may be timed
                # more than once per tick — sum them).
                self._current_tick[component_name] = (
                    self._current_tick.get(component_name, 0.0) + elapsed_ms
                )
        except Exception:  # noqa: BLE001 — profiling must never break a tick
            pass

    # ── Tick boundary ────────────────────────────────────────────────────
    def record_tick(self, total_ms: float) -> None:
        """Mark a completed tick with its total duration (ms). Never raises."""
        if not self.enabled:
            return
        try:
            total = float(total_ms)
            with self._lock:
                self._tick_ms.append(total)
                self._tick_count += 1
                if total > self.slow_tick_threshold_ms:
                    self._slow_tick_count += 1
                    # snapshot the heaviest components of this slow tick
                    top = sorted(
                        self._current_tick.items(), key=lambda kv: kv[1], reverse=True
                    )[:5]
                    self._slow_ticks.append({
                        "ms": round(total, 2),
                        "ts": time.time(),
                        "components": [
                            {"component": k, "ms": round(v, 2)} for k, v in top
                        ],
                    })
                self._current_tick = {}
        except Exception:  # noqa: BLE001
            pass

    # ── Reporting ────────────────────────────────────────────────────────
    def _component_stats_locked(self) -> List[dict]:
        rows: List[dict] = []
        for name, buf in self._component_ms.items():
            if not buf:
                continue
            ordered = sorted(buf)
            n = len(ordered)
            total = sum(ordered)
            rows.append({
                "component": name,
                "samples": n,
                "calls": int(self._component_calls.get(name, n)),
                "avg_ms": round(total / n, 3),
                "p50_ms": round(_percentile(ordered, 0.50), 3),
                "p95_ms": round(_percentile(ordered, 0.95), 3),
                "max_ms": round(ordered[-1], 3),
                "total_ms": round(total, 3),
            })
        rows.sort(key=lambda r: r["avg_ms"], reverse=True)
        return rows

    def _tick_stats_locked(self) -> dict:
        if not self._tick_ms:
            return {
                "samples": 0, "last_ms": 0.0, "avg_ms": 0.0,
                "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0,
                "tick_count": self._tick_count,
                "slow_tick_count": self._slow_tick_count,
                "slow_tick_threshold_ms": round(self.slow_tick_threshold_ms, 2),
            }
        ordered = sorted(self._tick_ms)
        n = len(ordered)
        return {
            "samples": n,
            "last_ms": round(self._tick_ms[-1], 2),
            "avg_ms": round(sum(ordered) / n, 2),
            "p50_ms": round(_percentile(ordered, 0.50), 2),
            "p95_ms": round(_percentile(ordered, 0.95), 2),
            "max_ms": round(ordered[-1], 2),
            "tick_count": self._tick_count,
            "slow_tick_count": self._slow_tick_count,
            "slow_tick_threshold_ms": round(self.slow_tick_threshold_ms, 2),
        }

    def get_profile_report(self) -> dict:
        """Per-component avg/p50/p95/max + total tick stats. Never raises."""
        try:
            with self._lock:
                return {
                    "enabled": self.enabled,
                    "window_size": self.window_size,
                    "tick": self._tick_stats_locked(),
                    "components": self._component_stats_locked(),
                }
        except Exception:  # noqa: BLE001
            return {"enabled": self.enabled, "tick": {}, "components": []}

    def get_optimization_recommendations(self) -> List[dict]:
        """Heuristic suggestions: which components dominate the tick budget.

        A component is flagged when its average duration is both a large
        fraction of the average tick AND above an absolute floor — those are the
        candidates to move to a slow-tick cadence or cache. Never raises.
        """
        recs: List[dict] = []
        try:
            with self._lock:
                tick = self._tick_stats_locked()
                comps = self._component_stats_locked()
            avg_tick = float(tick.get("avg_ms", 0.0) or 0.0)
            for c in comps:
                avg = float(c["avg_ms"])
                share = (avg / avg_tick) if avg_tick > 0 else 0.0
                # Dominant + non-trivial → recommend.
                if share >= 0.25 and avg >= 5.0:
                    recs.append({
                        "component": c["component"],
                        "avg_ms": avg,
                        "share_of_tick": round(share, 3),
                        "severity": "high" if share >= 0.5 else "medium",
                        "suggestion": (
                            f"{c['component']} averages {avg:.1f}ms "
                            f"({share:.0%} of the tick) — consider a slow-tick "
                            "cadence, caching, or batching."
                        ),
                    })
            recs.sort(key=lambda r: r["avg_ms"], reverse=True)
        except Exception:  # noqa: BLE001
            return []
        return recs

    def get_dashboard_data(self) -> dict:
        """Compact payload for the ops dashboard. Never raises."""
        report = self.get_profile_report()
        try:
            with self._lock:
                slow = list(self._slow_ticks)[-20:][::-1]
        except Exception:  # noqa: BLE001
            slow = []
        return {
            "enabled": self.enabled,
            "tick": report.get("tick", {}),
            "components": report.get("components", []),
            "slow_ticks": slow,
            "recommendations": self.get_optimization_recommendations(),
        }

    def reset(self) -> None:
        """Clear all accumulated stats. Never raises."""
        try:
            with self._lock:
                self._component_ms.clear()
                self._component_calls.clear()
                self._tick_ms.clear()
                self._slow_ticks.clear()
                self._current_tick = {}
                self._tick_count = 0
                self._slow_tick_count = 0
        except Exception:  # noqa: BLE001
            pass


__all__ = ["TickProfiler"]

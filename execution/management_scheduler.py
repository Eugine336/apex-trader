"""Event-reactive management scheduler (Phase 3).

Replaces the fixed-rate "evaluate every open position every 100 ms" sweep with
an **event-reactive** policy: a symbol's positions are re-evaluated shortly
after a tick arrives for that symbol, while idle symbols are still swept on a
slower safety-net cadence so time-based management (hold time, trailing, DE
re-checks) keeps running.

The scheduler is intentionally a *pure decision component* — it owns no
threads and touches no broker/market state.  Tick threads call
:meth:`note_event` (cheap); the evaluation loop calls :meth:`due` to learn
which symbols to evaluate this cycle.  This decoupling keeps heavy evaluation
off the tick-ingestion threads.

Two bounds shape the behavior:

* ``min_interval_s`` — coalesce bursts: after a symbol is evaluated it is not
  eligible again (even with new ticks) until this much time has passed.  This
  caps per-symbol evaluation frequency under a flood of ticks.
* ``max_interval_s`` — safety net: a symbol becomes due at least this often
  even with no ticks at all, so timeouts / trailing / periodic DE checks still
  fire on quiet instruments.

Thread-safe: a single lock guards the small pending-set and last-eval map.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterable, List, Optional


class ManagementScheduler:
    """Decide which symbols are due for management evaluation.

    Parameters
    ----------
    min_interval_s:
        Minimum spacing between evaluations of the same symbol (burst
        coalescing).  Defaults to 0.1 s (≈ the legacy 10 Hz cadence for active
        symbols).
    max_interval_s:
        Safety-net spacing — a symbol is forced due at least this often even
        without events.  Defaults to 1.0 s.
    time_fn:
        Monotonic clock source (injectable for tests).
    """

    def __init__(
        self,
        *,
        min_interval_s: float = 0.1,
        max_interval_s: float = 1.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if min_interval_s < 0 or max_interval_s < 0:
            raise ValueError("intervals must be non-negative")
        # The safety net must not be tighter than the burst floor.
        self._min = float(min_interval_s)
        self._max = max(float(max_interval_s), self._min)
        self._time = time_fn
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._last_eval: dict[str, float] = {}

    def note_event(self, symbol: str) -> None:
        """Record that a relevant event (tick / candle close) arrived for
        *symbol*.  Cheap and safe to call from tick-ingestion threads."""
        if not symbol:
            return
        with self._lock:
            self._pending.add(symbol)

    def due(
        self,
        symbols: Iterable[str],
        *,
        now: Optional[float] = None,
    ) -> List[str]:
        """Return the subset of *symbols* that should be evaluated now.

        *symbols* is the current set of symbols holding open positions, so the
        scheduler never returns a symbol with nothing to manage and the safety
        net is scoped to live symbols.  Returned symbols are marked evaluated
        (their timers reset and any pending event cleared) so a single call is
        idempotent within a cycle.
        """
        t = self._time() if now is None else now
        out: List[str] = []
        with self._lock:
            for s in symbols:
                if not s:
                    continue
                last = self._last_eval.get(s)
                elapsed = float("inf") if last is None else (t - last)
                has_event = s in self._pending
                if (has_event and elapsed >= self._min) or elapsed >= self._max:
                    out.append(s)
                    self._last_eval[s] = t
                    self._pending.discard(s)
        return out

    def forget(self, live_symbols: Iterable[str]) -> None:
        """Drop bookkeeping for symbols no longer holding positions, bounding
        memory across the lifetime of the process."""
        keep = {s for s in live_symbols if s}
        with self._lock:
            self._last_eval = {k: v for k, v in self._last_eval.items() if k in keep}
            self._pending &= keep

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

"""APEX TRADER — Provider budget accounting (finite free-tier quotas).

The GPU/Compute Constitution treats every external model/inference provider as a
FINITE, failure-prone resource (§22–§24, §28, §34): APEX must know a provider's
request/token limits, how much it has consumed, how much remains, and when a
window resets — and must NEVER blindly exhaust a scarce free tier.

* :class:`ProviderBudget` — a per-provider (or per-model) rolling-window meter for
  requests-per-minute / -per-day and tokens-per-minute / -per-day. A limit of 0
  (or ``None``) means "unlimited": the meter still counts usage for observability
  but never refuses, so an unconfigured provider behaves exactly as before.
  :meth:`admits` answers "may I spend a request costing ~N tokens right now?" and
  :meth:`blocked_until` says how long to wait when the answer is no — enough to
  bench the provider until its window frees rather than re-hammer it.
* :class:`BudgetLedger` — holds one :class:`ProviderBudget` per named provider so
  the orchestrator/dashboards can see the whole cognitive resource pool at once.

The exact limits come from configuration (the provider's real service behaviour),
never assumptions baked into the code (§22). Pure standard library; thread-safe;
fail-safe (a metering fault must never break a reasoning call). Accounting is
infrastructure — none of this decides a trade; it only decides whether a scarce
cognitive resource may be spent right now.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

# The two accounting windows, in seconds. Requests/tokens are metered against
# both a rolling minute and a rolling day (the shapes free tiers actually use).
_MINUTE = 60.0
_DAY = 86400.0


def estimate_tokens(system: str, user: str, max_output_tokens: int = 0) -> int:
    """Cheap pre-send token estimate: ~4 chars/token for the prompt plus the
    reserved output budget.

    Deliberately conservative (rounds the prompt up) so the budget guard errs
    toward protecting a scarce quota rather than overspending it. Fail-safe —
    returns 0 on odd input; never raises."""
    try:
        chars = len(system or "") + len(user or "")
    except TypeError:
        chars = 0
    prompt = (chars + 3) // 4
    try:
        out = max(0, int(max_output_tokens or 0))
    except (TypeError, ValueError):
        out = 0
    return int(prompt + out)


class ProviderBudget:
    """Rolling-window request/token meter for one provider. Thread-safe; fail-safe.

    ``rpm`` / ``rpd`` cap requests per minute / per day; ``tpm`` / ``tpd`` cap
    tokens per minute / per day. A limit ``<= 0`` is unlimited (metered but never
    enforced). Times are monotonic seconds; an explicit ``now`` may be injected
    everywhere for deterministic testing.
    """

    def __init__(
        self,
        *,
        rpm: int = 0,
        rpd: int = 0,
        tpm: int = 0,
        tpd: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rpm = self._sanitize(rpm)
        self.rpd = self._sanitize(rpd)
        self.tpm = self._sanitize(tpm)
        self.tpd = self._sanitize(tpd)
        self._clock = clock
        # Each event is (timestamp, tokens) for one SENT request. Kept in
        # ascending time order (append order); pruned to the day window.
        self._events: list[tuple[float, int]] = []
        self._lock = threading.Lock()

    @staticmethod
    def _sanitize(v: object) -> int:
        try:
            n = int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
        return n if n > 0 else 0

    @property
    def enforced(self) -> bool:
        """True when at least one dimension is actually capped."""
        return bool(self.rpm or self.rpd or self.tpm or self.tpd)

    # ── recording ────────────────────────────────────────────────────────────
    def record(self, tokens: int = 0, *, now: Optional[float] = None) -> None:
        """Log one SENT request that consumed ``tokens`` tokens. Fail-safe."""
        t = self._clock() if now is None else float(now)
        try:
            tok = max(0, int(tokens))
        except (TypeError, ValueError):
            tok = 0
        with self._lock:
            self._events.append((t, tok))
            self._prune_nolock(t)

    # ── queries ──────────────────────────────────────────────────────────────
    def admits(self, estimated_tokens: int = 0, *, now: Optional[float] = None) -> bool:
        """True when a request costing ~``estimated_tokens`` fits every cap now."""
        t = self._clock() if now is None else float(now)
        try:
            est = max(0, int(estimated_tokens))
        except (TypeError, ValueError):
            est = 0
        with self._lock:
            self._prune_nolock(t)
            return self._admits_nolock(est, t)

    def blocked_until(self, estimated_tokens: int = 0, *, now: Optional[float] = None) -> float:
        """Seconds to wait before a request costing ~``estimated_tokens`` fits.

        0.0 when it already fits. Otherwise the largest per-dimension wait until
        an in-window event ages out and frees enough capacity — enough to bench
        the provider rather than retry into the same throttle. Bounded to a day."""
        t = self._clock() if now is None else float(now)
        try:
            est = max(0, int(estimated_tokens))
        except (TypeError, ValueError):
            est = 0
        with self._lock:
            self._prune_nolock(t)
            if self._admits_nolock(est, t):
                return 0.0
            waits = [
                self._wait_requests_nolock(_MINUTE, self.rpm, t),
                self._wait_requests_nolock(_DAY, self.rpd, t),
                self._wait_tokens_nolock(_MINUTE, self.tpm, t, est),
                self._wait_tokens_nolock(_DAY, self.tpd, t, est),
            ]
            return min(_DAY, max(0.0, max(waits)))

    def remaining(self, *, now: Optional[float] = None) -> dict:
        """Remaining capacity per dimension (``None`` = unlimited). Fail-safe."""
        t = self._clock() if now is None else float(now)
        with self._lock:
            self._prune_nolock(t)
            rq_min, rq_day, tk_min, tk_day = self._usage_nolock(t)
            return {
                "rpm": None if self.rpm <= 0 else max(0, self.rpm - rq_min),
                "rpd": None if self.rpd <= 0 else max(0, self.rpd - rq_day),
                "tpm": None if self.tpm <= 0 else max(0, self.tpm - tk_min),
                "tpd": None if self.tpd <= 0 else max(0, self.tpd - tk_day),
            }

    def to_dict(self, *, now: Optional[float] = None) -> dict:
        t = self._clock() if now is None else float(now)
        with self._lock:
            self._prune_nolock(t)
            rq_min, rq_day, tk_min, tk_day = self._usage_nolock(t)
            blocked = 0.0 if self._admits_nolock(0, t) else min(
                _DAY,
                max(
                    0.0,
                    max(
                        self._wait_requests_nolock(_MINUTE, self.rpm, t),
                        self._wait_requests_nolock(_DAY, self.rpd, t),
                    ),
                ),
            )
            return {
                "enforced": self.enforced,
                "limits": {"rpm": self.rpm, "rpd": self.rpd, "tpm": self.tpm, "tpd": self.tpd},
                "used": {"rpm": rq_min, "rpd": rq_day, "tpm": tk_min, "tpd": tk_day},
                "blocked_seconds": round(blocked, 1),
            }

    # ── internals (assume the lock is held) ──────────────────────────────────
    def _prune_nolock(self, now: float) -> None:
        cutoff = now - _DAY
        events = self._events
        # Events are appended in ascending time order, so drop from the front.
        i = 0
        n = len(events)
        while i < n and events[i][0] < cutoff:
            i += 1
        if i:
            del events[:i]

    def _usage_nolock(self, now: float) -> tuple[int, int, int, int]:
        min_cut = now - _MINUTE
        rq_min = rq_day = tk_min = tk_day = 0
        for ts, tok in self._events:
            rq_day += 1
            tk_day += tok
            if ts >= min_cut:
                rq_min += 1
                tk_min += tok
        return rq_min, rq_day, tk_min, tk_day

    def _admits_nolock(self, est: int, now: float) -> bool:
        rq_min, rq_day, tk_min, tk_day = self._usage_nolock(now)
        if self.rpm > 0 and rq_min + 1 > self.rpm:
            return False
        if self.rpd > 0 and rq_day + 1 > self.rpd:
            return False
        if self.tpm > 0 and tk_min + est > self.tpm:
            return False
        if self.tpd > 0 and tk_day + est > self.tpd:
            return False
        return True

    def _in_window_nolock(self, window: float, now: float) -> list[tuple[float, int]]:
        cutoff = now - window
        return [ev for ev in self._events if ev[0] >= cutoff]

    def _wait_requests_nolock(self, window: float, limit: int, now: float) -> float:
        if limit <= 0:
            return 0.0
        inw = self._in_window_nolock(window, now)
        used = len(inw)
        if used + 1 <= limit:
            return 0.0
        # Need this many of the oldest in-window requests to age out so one more
        # fits. inw is ascending by time; the k-th oldest is at index k-1.
        k = used + 1 - limit
        oldest = inw[k - 1][0]
        return max(0.0, min(window, (oldest + window) - now))

    def _wait_tokens_nolock(self, window: float, limit: int, now: float, est: int) -> float:
        if limit <= 0:
            return 0.0
        inw = self._in_window_nolock(window, now)
        total = sum(tok for _, tok in inw)
        if total + est <= limit:
            return 0.0
        target = limit - est
        if target < 0:
            # The request alone exceeds the cap — best effort: wait the full
            # window (all current usage clears; still may not fit, but we never
            # hammer). The client falls back to another provider meanwhile.
            return window
        remaining = total
        for ts, tok in inw:  # age out oldest first
            remaining -= tok
            if remaining <= target:
                return max(0.0, min(window, (ts + window) - now))
        return window


class BudgetLedger:
    """A pool of named :class:`ProviderBudget` meters. Thread-safe; fail-safe.

    ``budget(name, ...)`` gets-or-creates a provider's meter (limits are fixed at
    first creation). Lets the orchestrator/dashboards see the whole cognitive
    resource pool (§32/§34) without each caller holding its own meter."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._budgets: dict[str, ProviderBudget] = {}
        self._clock = clock
        self._lock = threading.Lock()

    def budget(self, name: str, *, rpm: int = 0, rpd: int = 0,
               tpm: int = 0, tpd: int = 0) -> ProviderBudget:
        key = str(name or "")
        with self._lock:
            b = self._budgets.get(key)
            if b is None:
                b = ProviderBudget(rpm=rpm, rpd=rpd, tpm=tpm, tpd=tpd, clock=self._clock)
                self._budgets[key] = b
            return b

    def record(self, name: str, tokens: int = 0, *, now: Optional[float] = None) -> None:
        self.budget(name).record(tokens, now=now)

    def admits(self, name: str, estimated_tokens: int = 0, *,
               now: Optional[float] = None) -> bool:
        return self.budget(name).admits(estimated_tokens, now=now)

    def to_dict(self, *, now: Optional[float] = None) -> dict:
        with self._lock:
            budgets = dict(self._budgets)
        return {name: b.to_dict(now=now) for name, b in budgets.items()}


__all__ = ["ProviderBudget", "BudgetLedger", "estimate_tokens"]

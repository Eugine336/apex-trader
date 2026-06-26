"""APEX TRADER — Governance health assessor (the system thermostat).

Every learner in the Learning Division (⑦) is a heating element — each one
nudges a parameter. The Governance Division (⑧) already authorises each nudge
in isolation ("is this multiplier within bounds?"). What nobody asked, until
now, is the thermostat question: *across all the nudges that were approved, is
the room getting hotter or colder?*

:class:`HealthAssessor` answers that. It keeps a bounded window of recent trade
outcomes and, on demand, computes a few aggregate signals:

* **rolling EV** — mean realized R over the window. The single most important
  signal: is the system, as a whole, making money?
* **rolling win rate** — fraction of window trades that won (context only).
* **entry-rate trend** — trades/day over the last 7 days vs the prior 7 days.
  A ratio > 1 means the system is participating *more* — which, paired with a
  negative EV, is the classic learning-drift failure mode (the gates loosened,
  more trades get through, and they lose).
* **learner-enabled loss rate** — of the window trades that a learner adjustment
  let through, what fraction lost. This is the "learning let a loser through"
  quadrant.

From those it returns a :class:`HealthStatus`:

* ``CRITICAL`` — ``rolling_ev < 0`` **and** (entry-rate accelerating past the
  threshold **or** learner-enabled trades losing heavily). This is the band
  Governance freezes learning on.
* ``DEGRADED`` — ``rolling_ev < 0`` **or** learner-enabled trades losing more
  often than the degraded threshold. A visible warning that does not, on its
  own, pause learning.
* ``HEALTHY`` — everything else, including any window too small to judge.

Design principles (mirrors the rest of the Governance department):

* **Fail-safe.** Any internal error — in ``record_trade_close`` or ``assess`` —
  is swallowed and ``assess`` returns ``HEALTHY``. A monitoring fault must
  *never* block trading or freeze learning.
* **Behaviour-neutral on deploy.** Until the window holds enough trades to
  judge (``_MIN_SAMPLE``), the verdict is ``HEALTHY``. A fresh install changes
  nothing until genuine, sustained degradation appears.
* **Bounded memory.** Outcomes live in a ``deque(maxlen=max_history)`` — the
  buffer can never grow without bound over a long-running process.
* **Thread-safe.** An ``RLock`` guards all mutable state, so the trade-close
  path and a dashboard reader can touch it concurrently.

Leaf module — standard library + loguru + its own models only. It never imports
a broker, the WorldModel, or any brain module.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from loguru import logger

from governance.health_models import HealthAssessment, HealthStatus

# A window smaller than this yields HEALTHY regardless of the numbers — one or
# two noisy trades must never freeze the learning layer. Kept deliberately
# small so genuine degradation over a handful of trades is still caught.
_MIN_SAMPLE = 5

# A "day" in seconds, for the entry-rate trend's two 7-day windows.
_DAY_SECONDS = 86_400.0
_WEEK_SECONDS = 7.0 * _DAY_SECONDS


@dataclass
class _TradeOutcome:
    """One recorded close — the minimum the assessor needs."""

    __slots__ = ("realized_r", "entry_path", "learner_enabled", "ts")

    realized_r: float
    entry_path: str
    learner_enabled: bool
    ts: float


class HealthAssessor:
    """Rolling aggregate-health monitor for the learning layer."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        window_size: int = 20,
        max_history: int = 200,
        critical_entry_rate_threshold: float = 1.3,
        critical_learner_loss_rate: float = 0.65,
        degraded_learner_loss_rate: float = 0.55,
    ) -> None:
        self.enabled = bool(enabled)
        self._window = max(1, int(window_size))
        max_hist = max(self._window, int(max_history))
        self._critical_entry_rate = float(critical_entry_rate_threshold)
        self._critical_learner_loss = float(critical_learner_loss_rate)
        self._degraded_learner_loss = float(degraded_learner_loss_rate)

        self._trades: Deque[_TradeOutcome] = deque(maxlen=max_hist)
        self._lock = threading.RLock()

    # ── Ingest ──────────────────────────────────────────────────────────

    def record_trade_close(
        self,
        realized_r: float,
        entry_path: str = "",
        learner_enabled: bool = False,
    ) -> None:
        """Record one closed trade. Fail-safe — never raises to the caller."""
        if not self.enabled:
            return
        try:
            r = float(realized_r)
            if r != r:  # NaN guard (NaN != NaN)
                r = 0.0
            outcome = _TradeOutcome(
                realized_r=r,
                entry_path=str(entry_path or ""),
                learner_enabled=bool(learner_enabled),
                ts=time.time(),
            )
            with self._lock:
                self._trades.append(outcome)
        except Exception as exc:  # noqa: BLE001 — monitoring must never break the close path
            logger.debug("[health] record_trade_close ignored a fault: {}", exc)

    # ── Assess ──────────────────────────────────────────────────────────

    def assess(self) -> HealthAssessment:
        """Compute the current rolling health verdict.

        Fail-safe: any internal error returns a ``HEALTHY`` assessment so a
        monitoring fault can never pause trading or learning.
        """
        if not self.enabled:
            return HealthAssessment(health_status=HealthStatus.HEALTHY)
        try:
            return self._assess_locked()
        except Exception as exc:  # noqa: BLE001 — fail toward HEALTHY
            logger.debug("[health] assess failed — defaulting HEALTHY: {}", exc)
            return HealthAssessment(health_status=HealthStatus.HEALTHY)

    def _assess_locked(self) -> HealthAssessment:
        with self._lock:
            all_trades = list(self._trades)

        window = all_trades[-self._window:]
        sample = len(window)
        if sample == 0:
            return HealthAssessment(health_status=HealthStatus.HEALTHY, sample_size=0)

        rolling_ev = sum(t.realized_r for t in window) / sample
        wins = sum(1 for t in window if t.realized_r > 0.0)
        rolling_win_rate = wins / sample

        learner_trades = [t for t in window if t.learner_enabled]
        if learner_trades:
            learner_losses = sum(1 for t in learner_trades if t.realized_r <= 0.0)
            learner_loss_rate = learner_losses / len(learner_trades)
        else:
            learner_loss_rate = 0.0

        entry_rate_trend = self._entry_rate_trend(all_trades)

        status = self._classify(
            sample=sample,
            rolling_ev=rolling_ev,
            entry_rate_trend=entry_rate_trend,
            learner_loss_rate=learner_loss_rate,
        )

        return HealthAssessment(
            rolling_ev=rolling_ev,
            rolling_win_rate=rolling_win_rate,
            entry_rate_trend=entry_rate_trend,
            learner_enabled_loss_rate=learner_loss_rate,
            health_status=status,
            sample_size=sample,
        )

    def _entry_rate_trend(self, trades: list[_TradeOutcome]) -> float:
        """Trades in the last 7 days / trades in the prior 7 days.

        Returns 1.0 (neutral) when there is no prior-window baseline to compare
        against — a fresh install must not read as "accelerating" simply
        because its history does not yet span two weeks.
        """
        now = time.time()
        recent_cutoff = now - _WEEK_SECONDS
        prior_cutoff = now - 2.0 * _WEEK_SECONDS
        recent = sum(1 for t in trades if t.ts >= recent_cutoff)
        prior = sum(1 for t in trades if prior_cutoff <= t.ts < recent_cutoff)
        if prior <= 0:
            return 1.0
        return recent / prior

    def _classify(
        self,
        *,
        sample: int,
        rolling_ev: float,
        entry_rate_trend: float,
        learner_loss_rate: float,
    ) -> HealthStatus:
        # Behaviour-neutral until the window is large enough to judge.
        if sample < _MIN_SAMPLE:
            return HealthStatus.HEALTHY

        if rolling_ev < 0.0 and (
            entry_rate_trend > self._critical_entry_rate
            or learner_loss_rate > self._critical_learner_loss
        ):
            return HealthStatus.CRITICAL

        if rolling_ev < 0.0 or learner_loss_rate > self._degraded_learner_loss:
            return HealthStatus.DEGRADED

        return HealthStatus.HEALTHY

    # ── Introspection ───────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Summary for the dashboard / ops."""
        assessment = self.assess()
        with self._lock:
            history_n = len(self._trades)
        out = assessment.to_dict()
        out.update(
            {
                "enabled": self.enabled,
                "window_size": self._window,
                "history_size": history_n,
                "thresholds": {
                    "critical_entry_rate": self._critical_entry_rate,
                    "critical_learner_loss_rate": self._critical_learner_loss,
                    "degraded_learner_loss_rate": self._degraded_learner_loss,
                    "min_sample": _MIN_SAMPLE,
                },
            }
        )
        return out

    def reset(self) -> None:
        """Drop all recorded outcomes (used by tests / a manual ops reset)."""
        with self._lock:
            self._trades.clear()


__all__ = ["HealthAssessor"]

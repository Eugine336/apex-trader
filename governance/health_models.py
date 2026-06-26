"""APEX TRADER — Governance health-assessment data models.

Small, JSON-friendly carriers used by
:class:`governance.health_assessor.HealthAssessor` and surfaced by
:class:`governance.division.GovernanceDivision`.

The Governance Division (Department ⑧) authorises individual Learning
recommendations in isolation. The :class:`HealthAssessor` adds the *aggregate*
view it was missing — "across everything Learning has changed, is the system
getting better or worse?" — and these models are the typed result of that
assessment.

* :class:`HealthStatus` — the three-state verdict on aggregate system health.
* :class:`HealthAssessment` — the rolling-metric snapshot Governance reads to
  decide whether to freeze (pause) or release (resume) the learning layer.

Deliberately explicit, immutable-ish carriers (no global state, no broker, no
WorldModel) so the assessor stays a pure, unit-testable leaf.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class HealthStatus(str, Enum):
    """Aggregate system-health verdict (str-valued for trivial JSON / logging).

    * ``HEALTHY``  — the system (including the net effect of all learning) is
      performing acceptably; learning runs normally.
    * ``DEGRADED`` — a warning band: rolling expectancy has turned negative or
      learner-enabled trades are losing more often than not. Surfaced for
      visibility; on its own it does not pause learning.
    * ``CRITICAL`` — rolling expectancy is negative *and* the system is either
      accelerating its participation or its learner-enabled trades are losing
      heavily. This is the band that triggers an automatic learning freeze.
    """

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


@dataclass
class HealthAssessment:
    """One rolling snapshot of aggregate system health.

    All metrics are computed over a bounded recent window so the verdict tracks
    *current* behaviour rather than lifetime history. ``sample_size`` is the
    number of trades that actually informed the rolling metrics — a small
    sample yields ``HEALTHY`` (behaviour-neutral) so the system never freezes
    learning on one or two noisy trades.
    """

    rolling_ev: float = 0.0                 # mean realized R over the window
    rolling_win_rate: float = 0.0           # fraction of window trades with R > 0
    entry_rate_trend: float = 1.0           # recent-7d trades / prior-7d trades (>1 = accelerating)
    learner_enabled_loss_rate: float = 0.0  # fraction of learner-enabled window trades that lost
    health_status: HealthStatus = HealthStatus.HEALTHY
    sample_size: int = 0                    # trades that informed the window metrics
    assessed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "rolling_ev": round(float(self.rolling_ev), 4),
            "rolling_win_rate": round(float(self.rolling_win_rate), 4),
            "entry_rate_trend": round(float(self.entry_rate_trend), 4),
            "learner_enabled_loss_rate": round(float(self.learner_enabled_loss_rate), 4),
            "health_status": str(self.health_status),
            "sample_size": int(self.sample_size),
            "assessed_at": self.assessed_at,
        }


__all__ = ["HealthStatus", "HealthAssessment"]

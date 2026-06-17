"""
APEX TRADER — Portfolio Governor data models.

`GovernorConfig` holds the portfolio-level risk limits and `GovernorVerdict`
is the structured answer the governor returns for every entry consideration.

Leaf module — depends only on the standard library so it can be imported
from anywhere (including config.py) without circular-import risk.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class GovernorConfig:
    """Portfolio-level risk limits.

    Starting values are reasonable defaults — not claimed optimal.  All limits
    are advisory to the planner (defence in depth), never a hard single point
    of failure in the execution layer.
    """

    enabled: bool = True
    max_open_positions: int = 8  # hard cap on simultaneous positions
    max_currency_exposure: int = 3  # max positions touching one currency (e.g. 3 JPY pairs)
    max_correlated_positions: int = 2  # max same-signed exposure on a shared currency
    daily_loss_cap_pct: float = 3.0  # halt new entries after −this% daily drawdown
    daily_loss_recovery_pct: float = 1.5  # resume only once DD recovers above −this%
    max_sector_exposure: int = 4  # max positions in one category (forex/index/commodity/crypto)
    # #24 — accumulated-risk mode. When False the governor keeps its legacy
    # first-breach behaviour: the analytical concentration limits
    # (currency / sector / correlated exposure) HARD-block on the first breach.
    # When True those analytical limits become a graded *dimmer* instead — the
    # verdict stays ``allowed`` but carries a bounded ``risk_multiplier`` (folded
    # into sizing by the orchestrator) so a near-/over-limit concentration sizes
    # the trade DOWN rather than killing it. The physics caps (max open
    # positions) and the daily-loss safety halt always stay HARD regardless.
    # Default True reflects the live orchestrator-on mode (main_loop re-affirms
    # it from the orchestrator config).
    graded_exposure: bool = True
    # Lower bound on the graded risk multiplier — a graded "at every limit" is
    # still a small trade, never zero.
    risk_multiplier_floor: float = 0.15
    # When the governor's own code raises, fail CLOSED (block the trade) instead
    # of allowing it. A portfolio-risk veto that silently no-ops on a bug is a
    # safety gate you can't trust, so the default is fail-closed — consistent
    # with the decision pipeline. Set False to restore legacy fail-open (allow
    # on error) if a governor bug ever halts trading.
    fail_closed: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GovernorConfig":
        valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in valid})


@dataclass
class GovernorVerdict:
    """The governor's answer for one entry consideration."""

    allowed: bool
    reason: str
    blocked_by: Optional[str] = None  # which check blocked, None when allowed
    # #24 — accumulated-risk fields (graded mode). ``risk_multiplier`` is a
    # bounded [floor, 1.0] size dimmer the orchestrator folds into sizing when an
    # analytical concentration limit is near/over but the trade is still allowed;
    # 1.0 = clear of all analytical limits. ``near_breaches`` names the analytical
    # dimensions at/over their limit and ``risk_score`` (0..1) is the closeness of
    # the nearest limit — all surfaced on the trace / dashboard.
    risk_multiplier: float = 1.0
    risk_score: float = 0.0
    near_breaches: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "blocked_by": self.blocked_by,
            "risk_multiplier": round(self.risk_multiplier, 4),
            "risk_score": round(self.risk_score, 4),
            "near_breaches": list(self.near_breaches),
        }

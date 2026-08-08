"""APEX TRADER — Compliance Division verdict models.

The Compliance Division answers exactly one question: *is this trade
allowed?*  It returns a :class:`ComplianceVerdict` — APPROVED or REJECTED —
and never sizes a position, estimates profitability, or forms an opinion on
whether a trade is *wise* (that is the Portfolio Division's job).

Every individual permit check produces a :class:`CheckOutcome` so the caller
gets a complete diagnostic (all checks are evaluated, not short-circuited).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CheckOutcome:
    """Result of a single permit check.

    ``passed`` False means this check REJECTS the trade.  ``reason`` carries a
    human-readable explanation (the rejection cause when failed, ``"OK"`` /
    a short note when passed).
    """

    name: str
    passed: bool
    reason: str = "OK"


@dataclass
class ComplianceVerdict:
    """Aggregate permit decision for a candidate trade.

    ``approved`` is True only when **every** evaluated check passed.  When
    False, ``reasons`` lists every rejection cause (checks do not
    short-circuit, so all failures are reported for debugging).
    """

    approved: bool
    reasons: list[str] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)
    outcomes: list[CheckOutcome] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return not self.approved

    def summary(self) -> str:
        """One-line summary suitable for logging."""
        if self.approved:
            return "APPROVED"
        return "REJECTED — " + "; ".join(self.reasons)

    @classmethod
    def from_outcomes(cls, outcomes: list[CheckOutcome]) -> "ComplianceVerdict":
        """Build a verdict from the per-check outcomes."""
        approved = all(o.passed for o in outcomes)
        reasons = [f"{o.name}: {o.reason}" for o in outcomes if not o.passed]
        return cls(
            approved=approved,
            reasons=reasons,
            checks_run=[o.name for o in outcomes],
            outcomes=list(outcomes),
        )

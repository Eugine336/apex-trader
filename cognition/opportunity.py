"""Execution-side expected-return qualification.

The Brain supplies a producer-verified fixed-horizon expected directional return
in units of risk (R). Convert it to account currency using the verified value of
1R for the proposed size, then subtract fresh broker-truth round-trip costs.
Reasoning confidence is never interpreted as a win probability.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class OpportunityQualification:
    qualified: bool
    expected_net: float
    expected_gross: float
    cost: float
    risk_ccy: float
    expected_return_r: float
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "qualified": self.qualified,
            "expected_net": round(self.expected_net, 4),
            "expected_gross": round(self.expected_gross, 4),
            "cost": round(self.cost, 4),
            "risk_ccy": round(self.risk_ccy, 4),
            "expected_return_r": round(self.expected_return_r, 6),
            "reason": self.reason,
        }


def qualify_net_ev(
    *,
    expected_return_r: float,
    risk_ccy: float,
    cost_ccy: float,
    min_edge_ccy: float = 0.0,
    cost_multiple: float = 2.0,
) -> OpportunityQualification:
    """Qualify empirical expected return after current execution costs.

    expected_return_r is a producer-verified fixed-horizon directional return
    expressed in units of initial stop risk. risk_ccy is the account-currency
    value of 1R for this exact size. cost_ccy is current broker-truth round-trip
    cost. No confidence score or inferred probability is accepted.
    """
    try:
        expected_r = float(expected_return_r)
        risk = float(risk_ccy)
        raw_cost = float(cost_ccy)
        multiple = float(cost_multiple)
        min_edge = float(min_edge_ccy)
    except (TypeError, ValueError, OverflowError):
        return OpportunityQualification(False, 0.0, 0.0, 0.0, 0.0, 0.0, "invalid inputs")
    if (
        not all(math.isfinite(value) for value in (expected_r, risk, raw_cost, multiple, min_edge))
        or risk <= 0.0 or raw_cost < 0.0 or multiple < 0.0
    ):
        return OpportunityQualification(
            False, 0.0, 0.0, 0.0,
            max(0.0, risk) if math.isfinite(risk) else 0.0,
            expected_r if math.isfinite(expected_r) else 0.0,
            "non-finite or non-computable expected-return inputs",
        )
    cost = raw_cost * multiple
    expected_gross = expected_r * risk
    expected_net = expected_gross - cost
    qualified = expected_net > min_edge
    reason = (
        f"net EV {expected_net:.2f} = verified expected return "
        f"{expected_r:.4f}R × 1R value {risk:.2f} - cost {cost:.2f}; "
        + ("QUALIFIED" if qualified else "REJECTED — presume noise")
    )
    return OpportunityQualification(
        qualified, expected_net, expected_gross, cost, risk, expected_r, reason,
    )


__all__ = ["OpportunityQualification", "qualify_net_ev"]

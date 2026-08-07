"""APEX TRADER — Opportunity qualification (Constitution Part XIX, Articles 1/7/8).

The market is presumed noise (Art 1). A market read only becomes a tradeable
opportunity if it survives qualification on **positive expected NET value after
costs** (Art 7): expected gross edge minus the real round-trip cost of taking it
(spread + commission + slippage). Art 8's meta-reasoning ("would I still take
this if costs doubled?") is expressed as a ``cost_multiple`` applied to the cost
before the decision — so a trade that only survives on unrealistically cheap
execution is refused.

Pure and side-effect free (stdlib only) so it is fully offline-testable; the
execution layer supplies the broker-truth money conversion + cost estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def _clamp01(v: object, default: float = 0.0) -> float:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return 0.0 if f < 0.0 else (1.0 if f > 1.0 else f)


@dataclass
class OpportunityQualification:
    """The verdict of a net-EV opportunity qualification (Part XIX Art 7)."""

    qualified: bool
    expected_net: float          # expected value after cost×cost_multiple (ccy)
    expected_gross: float        # probability-weighted edge before cost (ccy)
    cost: float                  # round-trip cost used (ccy, already ×multiple)
    reward_ccy: float            # reward leg in ccy (|tp-entry| × money_per_price)
    risk_ccy: float              # risk leg in ccy (|entry-sl| × money_per_price)
    p_win: float
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "qualified": self.qualified,
            "expected_net": round(self.expected_net, 4),
            "expected_gross": round(self.expected_gross, 4),
            "cost": round(self.cost, 4),
            "reward_ccy": round(self.reward_ccy, 4),
            "risk_ccy": round(self.risk_ccy, 4),
            "p_win": round(self.p_win, 4),
            "reason": self.reason,
        }


def qualify_net_ev(
    *,
    entry: float,
    sl: float,
    tp: float,
    confidence: float,
    money_per_price: float,
    cost_ccy: float,
    min_edge_ccy: float = 0.0,
    cost_multiple: float = 2.0,
) -> OpportunityQualification:
    """Qualify a proposed entry by positive expected NET value (Part XIX Art 7).

    ``money_per_price`` converts one unit of price movement into account currency
    for the intended position size (``trade_tick_value / trade_tick_size × lots``).
    ``cost_ccy`` is the estimated round-trip cost (spread + commission + slippage)
    in account currency. ``confidence`` (0..1) is used as the success probability.

    Expected gross = ``p·reward − (1−p)·risk`` (in ccy); expected net = gross −
    ``cost_ccy × cost_multiple``. Qualified when expected net exceeds
    ``min_edge_ccy``. Never raises — an unqualifiable input returns
    ``qualified=False`` with a reason.
    """
    try:
        e = float(entry)
        s = float(sl)
        t = float(tp)
        mpp = float(money_per_price)
        cost = max(0.0, float(cost_ccy)) * max(0.0, float(cost_multiple))
    except (TypeError, ValueError):
        return OpportunityQualification(
            False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "invalid inputs",
        )
    p = _clamp01(confidence)
    risk_price = abs(e - s)
    reward_price = abs(t - e)
    if e <= 0.0 or mpp <= 0.0 or risk_price <= 0.0 or reward_price <= 0.0:
        return OpportunityQualification(
            False, 0.0, 0.0, cost, 0.0, 0.0, p,
            "non-computable levels/size",
        )
    reward_ccy = reward_price * mpp
    risk_ccy = risk_price * mpp
    expected_gross = p * reward_ccy - (1.0 - p) * risk_ccy
    expected_net = expected_gross - cost
    qualified = expected_net > float(min_edge_ccy)
    reason = (
        f"net EV {expected_net:.2f} = gross {expected_gross:.2f} - "
        f"cost {cost:.2f} (p={p:.2f}, R+{reward_ccy:.2f}/R-{risk_ccy:.2f}); "
        + ("QUALIFIED" if qualified else "REJECTED — presume noise")
    )
    return OpportunityQualification(
        qualified=qualified, expected_net=expected_net,
        expected_gross=expected_gross, cost=cost,
        reward_ccy=reward_ccy, risk_ccy=risk_ccy, p_win=p, reason=reason,
    )


__all__ = ["OpportunityQualification", "qualify_net_ev"]

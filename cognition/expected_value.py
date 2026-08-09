"""APEX TRADER — expected-value model for the cognitive decision (Part IX Art 1/7).

Pure standard library. Turns the Brain's win-probability and payoff geometry
into an expected value expressed in units of risk (R), so a decision carries a
genuine EV instead of reusing the ``confidence`` scalar as a proxy (the collapse
the constitution forbids, §XXIX / §VI Q13/Q17).

    EV_R = p_win * reward_r - (1 - p_win) * risk_r - cost_r

* ``p_win``   — the Brain's probability that the thesis plays out favorably, in
  [0, 1]. This is the precise meaning ``confidence`` is given at the decision
  layer: a probability of a favorable outcome, not an undefined scalar.
* ``reward_r``— favorable excursion in units of risk (take-profit distance ÷
  stop distance). ``risk_r`` is 1.0 by definition (R *is* the unit of risk)
  unless a caller supplies an explicit adverse excursion.
* ``cost_r``  — round-trip execution cost in R. Zero at the cognition layer; the
  execution sink applies the money-denominated cost with real spread/slippage
  (a decision that is EV-positive in R can still be declined there once cost is
  known — Part IX Q35/Q36).
"""

from __future__ import annotations

import re
from typing import Any, Optional

_NUM = re.compile(r"[-+]?\d*\.?\d+")


def _num(text: Any) -> Optional[float]:
    """First numeric magnitude in ``text`` (e.g. '1.8R' -> 1.8), or None."""
    try:
        m = _NUM.search(str(text if text is not None else ""))
        return float(m.group()) if m else None
    except (TypeError, ValueError):
        return None


def reward_risk_from_opinion(opinion: Any, default_reward_r: float) -> "tuple[float, float]":
    """Best-effort ``(reward_r, risk_r)`` from an opinion's excursion estimates.

    When the opinion's expected favorable AND adverse excursions both parse to
    positive magnitudes, the reward-to-risk ratio is ``favorable / adverse``
    against 1.0R of risk. Otherwise it falls back to the configured default
    reward multiple against 1.0R risk. Never raises.
    """
    risk_r = 1.0
    efe = _num(getattr(opinion, "expected_favorable_excursion", ""))
    eae = _num(getattr(opinion, "expected_adverse_excursion", ""))
    if efe is not None and eae is not None and efe > 0 and eae > 0:
        return (max(0.0, efe / eae), risk_r)
    reward_r = float(default_reward_r) if default_reward_r and default_reward_r > 0 else 1.0
    return (reward_r, risk_r)


def expected_value_r(
    p_win: float, reward_r: float, risk_r: float = 1.0, cost_r: float = 0.0,
) -> float:
    """Expected value in units of risk (R). Pure arithmetic; never raises."""
    try:
        p = min(1.0, max(0.0, float(p_win)))
        return p * float(reward_r) - (1.0 - p) * float(risk_r) - float(cost_r)
    except (TypeError, ValueError):
        return 0.0


# Part XXV — multidimensional confidence. A single scalar conflates four distinct
# questions; these decompose it so the decision layer can act on the profile.
_CONFIDENCE_DIMENSIONS = (
    "thesis_confidence",       # is the read of what's happening correct?
    "opportunity_confidence",  # is there a real exploitable edge?
    "timing_confidence",       # is NOW the moment, or is it early?
    "execution_confidence",    # can it be realised after spread/slippage/liquidity?
)


def _dim_value(opinion: Any, key: str) -> Optional[float]:
    """A single confidence dimension in [0, 1], or None when absent/unparseable."""
    v = getattr(opinion, key, None)
    if v is None and isinstance(opinion, dict):
        v = opinion.get(key)
    if v is None:
        return None
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return None


def effective_confidence(opinion: Any, fallback: Optional[float] = None) -> float:
    """Blend the multidimensional confidence into one conviction (Part XXV).

    A trade is only as strong as its WEAKEST dimension: a great thesis with poor
    execution or bad timing is not an executable edge *now*. This returns the
    MINIMUM of the present dimensions (thesis / opportunity / timing / execution),
    each defaulting to the opinion's overall ``confidence`` (or ``fallback``) when
    a model did not supply it. When a reply carries only the scalar, every
    dimension equals it and the minimum is exactly that scalar — so a legacy
    single-confidence opinion is unchanged (zero regression). Never raises.
    """
    try:
        base = getattr(opinion, "confidence", None)
        if base is None and isinstance(opinion, dict):
            base = opinion.get("confidence")
        if base is None:
            base = fallback
        base = min(1.0, max(0.0, float(base if base is not None else 0.0)))
    except (TypeError, ValueError):
        base = min(1.0, max(0.0, float(fallback))) if fallback is not None else 0.0
    dims = [d if d is not None else base for d in
            (_dim_value(opinion, k) for k in _CONFIDENCE_DIMENSIONS)]
    return min(dims) if dims else base


__all__ = ["expected_value_r", "reward_risk_from_opinion", "effective_confidence"]

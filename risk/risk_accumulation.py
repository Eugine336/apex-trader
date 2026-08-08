"""
APEX TRADER — Accumulated risk scoring.

Replaces the legacy *first-breach kill* pattern (evaluate risk dimensions in
order, return a hard veto on the FIRST one that trips, leaving every other
dimension invisible) with an *accumulated* read: every dimension is measured,
its proximity to its own limit is recorded, and the dimensions fold together
into

  * a ``score`` in ``[0, 1]`` — how close the nearest/heaviest limit is,
  * a bounded ``multiplier`` in ``[floor, 1.0]`` — a size dimmer the
    orchestrator folds into sizing (a near-limit dimension sizes the trade
    DOWN; it never extinguishes it), and
  * the full lists of breached / near-breach dimensions for the trace and
    dashboard.

Pure, dependency-free maths — no broker, network, pandas or torch — so it is
trivially unit-testable and safe on the live hot path.  This module NEVER
raises a hard veto; it only grades.  Genuine hard vetoes stay where they
belong (physics: broker margin, position limits, market closed) in the
callers, which keep those checks hard and use this module for everything
analytical.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Proximity at/above which a dimension is reported as a "near breach" (it has
# not tripped its limit yet but is close enough that it is already dimming).
NEAR_BREACH_AT = 0.8
# Per-dimension dimmer at exactly the limit (proximity == 1.0).  A dimension
# right at its limit is allowed through but sized to this fraction; past the
# limit it decays further toward the floor.
OVER_LIMIT_FACTOR = 0.6


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return f


def proximity(value: float, limit: float, *, lower_is_riskier: bool = False) -> float:
    """Proximity of ``value`` to ``limit`` as a ratio where ``1.0`` == at the
    limit and ``> 1.0`` == breached.

    By default a *higher* value is riskier (e.g. portfolio heat vs a heat cap):
    ``proximity = value / limit``.  When ``lower_is_riskier`` a *lower* value is
    riskier (e.g. risk:reward vs a minimum, free margin vs a floor):
    ``proximity = limit / value``.  Clamped to ``>= 0``; a non-positive or
    invalid denominator returns ``0.0`` (treated as no risk so a missing read
    never fabricates a breach).
    """
    value = _safe_float(value)
    limit = _safe_float(limit)
    if lower_is_riskier:
        if value <= 0:
            # No/negative value where higher is safer — treat as fully breached.
            return 2.0 if limit > 0 else 0.0
        return max(0.0, limit / value)
    if limit <= 0:
        return 0.0
    return max(0.0, value / limit)


@dataclass
class RiskDimension:
    """One risk dimension measured against its own limit."""

    name: str
    proximity: float          # 0..; >= 1.0 means the limit is breached
    value: float
    limit: float
    reason: str = ""

    @property
    def breached(self) -> bool:
        return self.proximity >= 1.0

    @property
    def near_breach(self) -> bool:
        return NEAR_BREACH_AT <= self.proximity < 1.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "proximity": round(self.proximity, 4),
            "value": round(self.value, 6),
            "limit": round(self.limit, 6),
            "breached": self.breached,
            "near_breach": self.near_breach,
            "reason": self.reason,
        }


def dimension(
    name: str,
    value: float,
    limit: float,
    reason: str = "",
    *,
    lower_is_riskier: bool = False,
) -> RiskDimension:
    """Build a :class:`RiskDimension` from a raw value + its limit."""
    p = proximity(value, limit, lower_is_riskier=lower_is_riskier)
    return RiskDimension(
        name=name,
        proximity=p,
        value=_safe_float(value),
        limit=_safe_float(limit),
        reason=reason,
    )


def _dim_factor(
    prox: float,
    *,
    near_breach_at: float,
    over_limit_factor: float,
    floor: float,
) -> float:
    """The bounded size factor a single dimension contributes.

    Plenty of headroom (``prox <= near_breach_at``) → ``1.0`` (no penalty).
    Approaching the limit → linear dim from ``1.0`` down to ``over_limit_factor``
    at the limit.  Past the limit → decay from ``over_limit_factor`` toward the
    ``floor`` as the breach deepens.  Never below ``floor``, never above ``1.0``.
    """
    if prox <= near_breach_at:
        return 1.0
    if prox < 1.0:
        span = max(1e-9, 1.0 - near_breach_at)
        frac = (prox - near_breach_at) / span
        return 1.0 - frac * (1.0 - over_limit_factor)
    # prox >= 1.0 — over the limit, decay further.
    return max(floor, over_limit_factor / (1.0 + (prox - 1.0)))


@dataclass
class RiskScore:
    """The accumulated read across every measured risk dimension."""

    score: float                              # 0..1 — closeness to nearest limit
    multiplier: float                         # [floor, 1.0] size dimmer
    dimensions: list[RiskDimension] = field(default_factory=list)
    floor: float = 0.15

    @property
    def near_breaches(self) -> list[str]:
        return [d.name for d in self.dimensions if d.near_breach]

    @property
    def breaches(self) -> list[str]:
        return [d.name for d in self.dimensions if d.breached]

    @property
    def has_breach(self) -> bool:
        return any(d.breached for d in self.dimensions)

    def summary(self) -> str:
        parts = ", ".join(
            f"{d.name}@{d.proximity:.0%}" for d in self.dimensions
        )
        return f"risk×{self.multiplier:.2f} score={self.score:.2f} [{parts}]"

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "multiplier": round(self.multiplier, 4),
            "near_breaches": self.near_breaches,
            "breaches": self.breaches,
            "dimensions": [d.to_dict() for d in self.dimensions],
        }


def accumulate(
    dimensions: list[RiskDimension],
    *,
    floor: float = 0.15,
    near_breach_at: float = NEAR_BREACH_AT,
    over_limit_factor: float = OVER_LIMIT_FACTOR,
) -> RiskScore:
    """Fold measured risk dimensions into a graded score + size multiplier.

    The multiplier is the product of every dimension's bounded factor (so being
    near two limits dims more than being near one), clamped to ``[floor, 1.0]``.
    The score is the single closest proximity clamped to ``[0, 1]`` — a quick
    read of "how close is the nearest limit?".  An empty list returns a neutral
    ``multiplier=1.0``/``score=0.0`` (no dimension → no dimming).
    """
    floor = _safe_float(floor, 0.15)
    floor = max(0.0, min(1.0, floor))
    dims = list(dimensions or [])
    if not dims:
        return RiskScore(score=0.0, multiplier=1.0, dimensions=[], floor=floor)

    product = 1.0
    max_prox = 0.0
    for d in dims:
        product *= _dim_factor(
            d.proximity,
            near_breach_at=near_breach_at,
            over_limit_factor=over_limit_factor,
            floor=floor,
        )
        if d.proximity > max_prox:
            max_prox = d.proximity
    multiplier = max(floor, min(1.0, product))
    score = max(0.0, min(1.0, max_prox))
    return RiskScore(
        score=score,
        multiplier=multiplier,
        dimensions=dims,
        floor=floor,
    )

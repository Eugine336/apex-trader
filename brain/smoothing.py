"""
APEX TRADER — Continuous smoothing helpers.

Shared, pure-math primitives for replacing discontinuous step functions
(``if x >= 0.9: 1.0 elif x >= 0.75: 0.85 ...``) with smooth, monotonic
curves. A value of 0.49 should earn ~95% of a bonus, not 0%; a conviction
of 0.879 and 0.851 should NOT both collapse onto the same 0.7 tier.

Design goals:
  * No discontinuities at the old tier boundaries.
  * Preserve the original tier values at their *representative* points
    (anchor a piecewise-linear curve on the old midpoints) so behaviour is
    unchanged at centres — the change is at the boundaries only.
  * Pure functions — only ``math``, no side effects, no I/O.
"""

from __future__ import annotations

import math
from typing import Sequence


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into ``[lo, hi]`` (handles lo > hi defensively)."""
    if hi < lo:
        lo, hi = hi, lo
    return max(lo, min(hi, value))


def piecewise_linear(x: float, points: Sequence[tuple[float, float]]) -> float:
    """Monotonic-in-x linear interpolation through ``(x, y)`` anchor points.

    ``points`` must be sorted ascending by x. Values of ``x`` below the first
    anchor clamp to the first y; values above the last clamp to the last y.
    This is the workhorse that replaces bucketed step functions: anchor the
    curve on the old tier values and the boundaries become continuous ramps.
    """
    if not points:
        return 0.0
    if x <= points[0][0]:
        return float(points[0][1])
    if x >= points[-1][0]:
        return float(points[-1][1])
    for i in range(1, len(points)):
        x1, y1 = points[i]
        if x <= x1:
            x0, y0 = points[i - 1]
            span = x1 - x0
            if span <= 0:
                return float(y1)
            t = (x - x0) / span
            return float(y0 + (y1 - y0) * t)
    return float(points[-1][1])


def smoothstep(x: float, edge0: float, edge1: float) -> float:
    """Cubic Hermite smoothstep — 0 below ``edge0``, 1 above ``edge1``, with a
    smooth (continuous first-derivative) transition in between."""
    if edge1 == edge0:
        return 0.0 if x < edge0 else 1.0
    t = clamp((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def logistic_between(
    x: float,
    lo: float,
    hi: float,
    midpoint: float,
    steepness: float = 10.0,
) -> float:
    """Logistic (sigmoid) interpolation between ``lo`` and ``hi`` centred at
    ``midpoint``. ``steepness`` controls how sharply it transitions — higher
    approximates a step, lower is a gentle ramp. Always continuous."""
    try:
        z = -steepness * (x - midpoint)
        # Guard against overflow in exp for large |z|.
        if z > 60.0:
            sig = 0.0
        elif z < -60.0:
            sig = 1.0
        else:
            sig = 1.0 / (1.0 + math.exp(z))
    except (OverflowError, ValueError):
        sig = 0.0 if x < midpoint else 1.0
    return lo + (hi - lo) * sig


def soft_ramp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """Linear ramp from ``y0`` (at ``x0``) to ``y1`` (at ``x1``), clamped flat
    outside ``[x0, x1]``. Equivalent to a two-anchor :func:`piecewise_linear`."""
    return piecewise_linear(x, ((x0, y0), (x1, y1)))

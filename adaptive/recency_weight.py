"""
APEX TRADER — Recency Weighting Helpers

Shared pure helpers for time-weighted learner statistics. When the recency
window falls back to the full trade history (a young account, or a long quiet
stretch), the AdaptiveOptimizer tags each trade with a ``_recency_weight`` so
older trades — possibly from a regime that no longer applies — contribute less
than fresh ones. Learners that understand the field compute weighted stats;
those that don't keep their existing unweighted behaviour (the field is simply
ignored), so every change here is additive and backward-compatible.
"""

from __future__ import annotations

import math
from typing import Sequence

# The key the AdaptiveOptimizer stamps onto a trade dict when it falls back to
# the full history with time-decay weighting. Centralised so producer and
# consumers can never drift apart.
RECENCY_WEIGHT_KEY = "_recency_weight"


def trade_weight(trade: dict) -> float:
    """Return a trade's recency weight, defaulting to a neutral ``1.0``.

    Any missing / non-finite / negative value collapses to ``1.0`` so a
    malformed weight can never zero-out or invert a trade's contribution.
    """
    raw = trade.get(RECENCY_WEIGHT_KEY, 1.0)
    try:
        w = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(w) or w < 0.0:
        return 1.0
    return w


def has_recency_weights(trades: Sequence[dict]) -> bool:
    """True when at least one trade carries the recency-weight field."""
    return any(RECENCY_WEIGHT_KEY in t for t in trades)


def weighted_win_rate(pnls: Sequence[float], weights: Sequence[float]) -> float:
    """Weighted win rate: Σw(wins) / Σw(decided). Scratches (pnl==0) excluded.

    Returns ``0.0`` when no decided (non-scratch) trades exist.
    """
    win_w = 0.0
    decided_w = 0.0
    for pnl, w in zip(pnls, weights):
        if pnl > 0:
            win_w += w
            decided_w += w
        elif pnl < 0:
            decided_w += w
    return (win_w / decided_w) if decided_w > 0 else 0.0


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    """Weighted arithmetic mean. Returns ``0.0`` when total weight is zero."""
    total_w = 0.0
    acc = 0.0
    for v, w in zip(values, weights):
        acc += v * w
        total_w += w
    return (acc / total_w) if total_w > 0 else 0.0

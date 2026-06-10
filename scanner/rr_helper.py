"""Pure-function reward/risk helper — no heavy dependencies (no torch)."""

from __future__ import annotations


def compute_side_agnostic_rr(
    buy_price: float | None,
    sell_price: float | None,
    current_price: float,
    atr_pips: float | None,
    pip_size: float,
) -> float | None:
    """Direction-agnostic RR from the nearest structural objective on either side.

    Uses the *smaller* of the two nearest-objective distances as the reward
    proxy (nearest realistic target) and 1.0×ATR as the risk proxy.
    Returns None when data is missing so the caller can hit a degraded path.
    """
    if atr_pips is None or atr_pips <= 0 or pip_size <= 0:
        return None

    buy_dist = sell_dist = None
    if buy_price is not None:
        buy_dist = abs(current_price - buy_price) / pip_size
    if sell_price is not None:
        sell_dist = abs(current_price - sell_price) / pip_size

    candidates = [d for d in (buy_dist, sell_dist) if d is not None and d > 0]
    if not candidates:
        return None

    reward_proxy = min(candidates)
    risk_proxy = atr_pips
    rr = reward_proxy / risk_proxy
    return max(0.0, min(rr, 5.0))

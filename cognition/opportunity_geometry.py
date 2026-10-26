"""Market-derived payoff geometry and activation evidence for opportunities.

This module contains no strategy preference or fixed reward multiple. It derives
entry/stop/target geometry from fresh completed-candle market-lens evidence and
requires an observable activation condition. Missing geometry or stale inputs
produce no qualification, allowing callers to fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Optional

from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION


def _finite_positive(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) and number > 0.0 else None


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


@dataclass(frozen=True)
class OpportunityGeometry:
    """Completed-bar market geometry for one directional opportunity."""

    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    risk_distance: float
    reward_distance: float
    reward_r: float
    activated: bool
    activation_reason: str
    invalidation_reason: str
    evidence_ids: tuple[str, ...]
    entry_timeframe: str = ""
    stop_timeframe: str = ""
    target_timeframe: str = ""
    identity_version: str = IDENTITY_VERSION
    identity: str = IDENTITY_STATEMENT
    symbol: str = ""
    evidence_timestamp_epoch: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_version": self.identity_version,
            "identity": self.identity,
            "symbol": self.symbol,
            "evidence_timestamp_epoch": self.evidence_timestamp_epoch,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "risk_distance": self.risk_distance,
            "reward_distance": self.reward_distance,
            "reward_r": self.reward_r,
            "activated": self.activated,
            "activation_reason": self.activation_reason,
            "invalidation_reason": self.invalidation_reason,
            "evidence_ids": list(self.evidence_ids),
            "entry_timeframe": self.entry_timeframe,
            "stop_timeframe": self.stop_timeframe,
            "target_timeframe": self.target_timeframe,
        }


def derive_opportunity_geometry(
    market_state: Any,
    direction: str,
    *,
    now: Optional[float] = None,
) -> Optional[OpportunityGeometry]:
    """Derive geometry only from fresh, timestamp-confirmed market evidence.

    Entry is the latest confirmed structural close. The protective boundary is
    the closest observed structural level on the adverse side; the target is the
    nearest observed structural level in the favorable direction. No 1R/2R
    default or guessed ATR multiple is introduced.

    Activation requires a directional completed-bar breakout, a trend plus
    same-direction completed-bar momentum, a measured range rotation at an edge
    with a fresh response, or a counter-side sweep/reclaim.
    Unavailable, stale, conflicting, or incomplete inputs return None.
    """
    side = str(direction or "").strip().upper()
    if side not in {"LONG", "SHORT"} or market_state is None:
        return None

    try:
        fresh = list(market_state.fresh_evidence(now))
    except Exception:
        return None

    state_symbol = str(getattr(market_state, "symbol", "") or "").strip().upper()
    if not state_symbol:
        return None
    structures: list[dict[str, Any]] = []
    momenta: list[dict[str, Any]] = []
    liquidity: list[dict[str, Any]] = []
    for item in fresh:
        evidence_symbol = str(getattr(item, "symbol", "") or "").strip().upper()
        if state_symbol and evidence_symbol and evidence_symbol != state_symbol:
            continue
        measurements = getattr(item, "measurements", None)
        if not isinstance(measurements, dict):
            continue
        # Only completed market-lens facts with known source timestamps can
        # define price levels or satisfy activation.
        if (
            measurements.get("source_kind") != "market_lens"
            or measurements.get("source_timestamp_known") is not True
        ):
            continue
        domain_value = getattr(getattr(item, "domain", None), "value", None)
        domain = str(domain_value or getattr(item, "domain", "") or "").lower()
        source_time = _finite(getattr(item, "timestamp_epoch", None))
        if source_time is None:
            continue
        if domain == "structure":
            price = _finite_positive(measurements.get("current_close"))
            high = _finite_positive(measurements.get("prior_range_high"))
            low = _finite_positive(measurements.get("prior_range_low"))
            if price is None or high is None or low is None or not high > low:
                continue
            structures.append({
                "item": item,
                "time": source_time,
                "price": price,
                "high": high,
                "low": low,
                "range_position": _finite(measurements.get("range_position")),
                "breakout_direction": str(measurements.get("breakout_direction", "") or "").lower(),
                "trend_state": str(measurements.get("trend_state", "") or "").lower(),
                "trend_strength": _finite(measurements.get("trend_strength")),
                "range_rotation": bool(measurements.get("range_rotation", False)),
                "upper_wick": _finite(measurements.get("upper_wick")),
                "lower_wick": _finite(measurements.get("lower_wick")),
                "timeframe": str(measurements.get("timeframe", "") or ""),
            })
        elif domain == "momentum":
            change = _finite(measurements.get("close_change"))
            if change is not None:
                momenta.append({
                    "item": item, "time": source_time, "change": change,
                    "timeframe": str(measurements.get("timeframe", "") or ""),
                })
        elif domain == "liquidity":
            liquidity.append({
                "item": item,
                "time": source_time,
                "side": str(measurements.get("sweep_side", "") or "").lower(),
                "close": _finite_positive(measurements.get("current_close")),
                "low": _finite_positive(measurements.get("prior_range_low")),
                "high": _finite_positive(measurements.get("prior_range_high")),
            })

    if not structures:
        return None
    structures.sort(key=lambda record: record["time"], reverse=True)
    entry_record = structures[0]
    entry = entry_record["price"]

    if side == "LONG":
        stops = [record for record in structures if record["low"] < entry]
        targets = [record for record in structures if record["high"] > entry]
        stop_record = max(stops, key=lambda record: record["low"]) if stops else None
        target_record = min(targets, key=lambda record: record["high"]) if targets else None
    else:
        stops = [record for record in structures if record["high"] > entry]
        targets = [record for record in structures if record["low"] < entry]
        stop_record = min(stops, key=lambda record: record["high"]) if stops else None
        target_record = max(targets, key=lambda record: record["low"]) if targets else None

    if stop_record is None or target_record is None:
        return None
    stop = stop_record["low"] if side == "LONG" else stop_record["high"]
    target = target_record["high"] if side == "LONG" else target_record["low"]
    risk_distance = entry - stop if side == "LONG" else stop - entry
    reward_distance = target - entry if side == "LONG" else entry - target
    if (
        not isfinite(risk_distance) or not isfinite(reward_distance)
        or risk_distance <= 0.0 or reward_distance <= 0.0
    ):
        return None

    direction_word = "up" if side == "LONG" else "down"
    matching_breakout = [
        record for record in structures
        if record["breakout_direction"] == direction_word
    ]
    matching_trend = [
        record for record in structures
        if record["trend_state"] == ("uptrend" if side == "LONG" else "downtrend")
        and (record["trend_strength"] or 0.0) > 0.0
    ]
    matching_momentum = [
        record for record in momenta
        if (record["change"] > 0.0 if side == "LONG" else record["change"] < 0.0)
    ]
    matching_sweep_side = "sell" if side == "LONG" else "buy"
    matching_sweeps = [
        item for item in liquidity
        if item["side"] == matching_sweep_side
    ]
    # Range rotation is a distinct market state, not a pattern-name gate. It
    # requires a measured range edge plus a fresh response in the candidate
    # direction; merely being in a range or touching an edge is insufficient.
    matching_range_rotation = [
        record for record in structures
        if record["trend_state"] == "range"
        and record["range_rotation"]
        and (
            (record["range_position"] is not None and record["range_position"] <= 0.15)
            if side == "LONG" else
            (record["range_position"] is not None and record["range_position"] >= 0.85)
        )
    ]
    range_response = bool(matching_range_rotation and matching_momentum)

    if matching_breakout:
        activation_reason = (
            f"completed-bar structural breakout {direction_word} "
            f"observed on {matching_breakout[0]['timeframe'] or 'a confirmed timeframe'}"
        )
    elif matching_trend and matching_momentum:
        activation_reason = (
            f"confirmed {matching_trend[0]['trend_state']} structure and "
            f"same-direction completed-bar momentum"
        )
    elif range_response:
        activation_reason = (
            f"measured range rotation at the {'lower' if side == 'LONG' else 'upper'} "
            "boundary with a fresh directional response"
        )
    elif matching_sweeps:
        activation_reason = (
            f"counter-side liquidity sweep/reclaim observed for {side}; "
            "retain uncertainty until independent market-domain gates are satisfied"
        )
    else:
        return None

    ids = {
        str(getattr(entry_record["item"], "evidence_id", "") or ""),
        str(getattr(stop_record["item"], "evidence_id", "") or ""),
        str(getattr(target_record["item"], "evidence_id", "") or ""),
    }
    for records in (matching_breakout[:1], matching_trend[:1], matching_momentum[:1]):
        for record in records:
            if "item" in record:
                ids.add(str(getattr(record["item"], "evidence_id", "") or ""))
    for record in matching_sweeps[:1]:
        ids.add(str(getattr(record["item"], "evidence_id", "") or ""))
    for record in matching_range_rotation[:1]:
        ids.add(str(getattr(record["item"], "evidence_id", "") or ""))
    ids.discard("")

    return OpportunityGeometry(
        direction=side,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        risk_distance=risk_distance,
        reward_distance=reward_distance,
        reward_r=reward_distance / risk_distance,
        activated=True,
        activation_reason=activation_reason,
        invalidation_reason=(
            f"{side} thesis invalidated by completed-bar acceptance "
            f"{'below' if side == 'LONG' else 'above'} structural stop {stop:.10g}"
        ),
        evidence_ids=tuple(sorted(ids)),
        entry_timeframe=entry_record["timeframe"],
        stop_timeframe=stop_record["timeframe"],
        target_timeframe=target_record["timeframe"],
        symbol=state_symbol,
        evidence_timestamp_epoch=entry_record["time"],
    )


__all__ = ["OpportunityGeometry", "derive_opportunity_geometry"]

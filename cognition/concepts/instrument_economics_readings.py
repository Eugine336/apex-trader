"""Broker-verified instrument mechanics for the native Brain.

The reader describes the contract specification actually returned by the
connected broker. It does not substitute configured fallbacks, infer absent
contract fields, or authorize execution.
"""
from __future__ import annotations

import math
from typing import Any

from cognition.concepts.base import ConceptReading, reference_price
from cognition.contracts import EvidenceDomain


_NUMERIC_FIELDS = (
    "trade_contract_size", "trade_tick_size", "trade_tick_value",
    "trade_tick_value_profit", "trade_tick_value_loss", "volume_min",
    "volume_max", "volume_step", "digits", "point",
    "trade_stops_level", "trade_freeze_level",
)
_REQUIRED_POSITIVE_FIELDS = (
    "trade_contract_size", "trade_tick_size", "volume_min",
    "volume_max", "volume_step", "point",
)
_TRADE_MODES = {
    0: "disabled", 1: "long_only", 2: "short_only",
    3: "close_only", 4: "full",
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _step_aligned(value: float, step: float) -> bool:
    if step <= 0:
        return False
    multiple = value / step
    return math.isclose(multiple, round(multiple), rel_tol=1e-9, abs_tol=1e-8)


class InstrumentEconomicsReadings:
    key = "instrument_economics_readings"
    family = "instrument_mechanics"
    covers_methods = ("instrument_economics",)
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        return self.read_with_context(symbol, bars_by_tf, {})

    def read_with_context(
        self,
        symbol: str,
        bars_by_tf: dict[str, list[dict]],
        context: dict[str, Any],
    ) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        raw = context.get("symbol_spec") if isinstance(context, dict) else None
        spec = raw if isinstance(raw, dict) else {}
        source = str(spec.get("source") or "unavailable")
        verified = (
            spec.get("source_verified") is True
            and spec.get("specification_verified") is True
            and bool(source.strip())
        )
        unknowns: list[str] = []
        errors: list[str] = []
        if not verified:
            unknowns.append(
                "live broker-verified symbol_info contract specification for this instrument"
            )
        if spec and verified and spec.get("source_timestamp_epoch") is None:
            unknowns.append(
                "provider-originated specification timestamp; retrieval time is not a verified source timestamp"
            )

        values = {key: _number(spec.get(key)) for key in _NUMERIC_FIELDS}
        for key in _REQUIRED_POSITIVE_FIELDS:
            value = values[key]
            if value is None:
                unknowns.append(f"broker specification field {key}")
            elif value <= 0:
                errors.append(f"{key} must be positive")
        for key in ("trade_tick_value", "trade_tick_value_profit", "trade_tick_value_loss"):
            value = values[key]
            if value is None:
                unknowns.append(f"broker specification field {key}")
            elif value < 0:
                errors.append(f"{key} cannot be negative")

        digits = values["digits"]
        if digits is None or digits < 0 or not digits.is_integer():
            unknowns.append("non-negative integer broker price precision (digits)")
        point = values["point"]
        if digits is not None and digits >= 0 and digits.is_integer() and point is not None and point > 0:
            if not math.isclose(point, 10.0 ** (-int(digits)), rel_tol=1e-7, abs_tol=1e-12):
                errors.append("broker point does not match the declared decimal precision")

        minimum, maximum, step = (
            values["volume_min"], values["volume_max"], values["volume_step"]
        )
        volume_constraints = {
            "minimum": minimum,
            "maximum": maximum,
            "step": step,
            "ordered": None,
            "minimum_step_aligned": None,
            "maximum_step_aligned": None,
        }
        if minimum is not None and maximum is not None:
            volume_constraints["ordered"] = minimum <= maximum
            if minimum > maximum:
                errors.append("volume_min exceeds volume_max")
        if step is not None and step > 0 and minimum is not None:
            volume_constraints["minimum_step_aligned"] = _step_aligned(minimum, step)
            if not volume_constraints["minimum_step_aligned"]:
                errors.append("volume_min is not aligned to volume_step")
        if step is not None and step > 0 and maximum is not None:
            volume_constraints["maximum_step_aligned"] = _step_aligned(maximum, step)
            if not volume_constraints["maximum_step_aligned"]:
                errors.append("volume_max is not aligned to volume_step")

        stops, freeze = values["trade_stops_level"], values["trade_freeze_level"]
        stop_distance = (
            stops * point if stops is not None and stops >= 0 and point is not None and point > 0 else None
        )
        freeze_distance = (
            freeze * point if freeze is not None and freeze >= 0 and point is not None and point > 0 else None
        )
        if stops is None or stops < 0:
            unknowns.append("non-negative broker trade_stops_level")
        if freeze is None or freeze < 0:
            unknowns.append("non-negative broker trade_freeze_level")

        tick_size, tick_value = values["trade_tick_size"], values["trade_tick_value"]
        value_per_price_unit = (
            tick_value / tick_size
            if tick_size is not None and tick_size > 0
            and tick_value is not None and tick_value > 0 else None
        )
        profit_tick, loss_tick = values["trade_tick_value_profit"], values["trade_tick_value_loss"]
        tick_value_asymmetry = (
            profit_tick != loss_tick
            if profit_tick is not None and loss_tick is not None else None
        )

        raw_mode = spec.get("trade_mode")
        mode_number = _number(raw_mode)
        trade_mode = (
            _TRADE_MODES.get(int(mode_number))
            if mode_number is not None and mode_number.is_integer() else None
        )
        if trade_mode is None:
            unknowns.append("broker trade_mode and its supported-operation semantics")
        for field in ("currency_base", "currency_profit", "currency_margin"):
            if not str(spec.get(field) or "").strip():
                unknowns.append(f"broker specification field {field}")

        if not verified:
            state = "unavailable_or_unverified"
        elif errors:
            state = "broker_specification_inconsistent"
        elif unknowns:
            state = "verified_but_incomplete"
        else:
            state = "verified_complete"

        facts = {
            "status": state,
            "source": source,
            "source_verified": verified,
            "specification_verified": spec.get("specification_verified") is True,
            "broker_symbol": spec.get("broker_symbol"),
            "description": spec.get("description"),
            "path": spec.get("path"),
            "trade_mode": raw_mode,
            "trade_mode_interpretation": trade_mode,
            "trade_calc_mode": spec.get("trade_calc_mode"),
            "trade_execution_mode": spec.get("trade_exemode"),
            "filling_mode": spec.get("filling_mode"),
            "order_mode": spec.get("order_mode"),
            "expiration_mode": spec.get("expiration_mode"),
            "contract_size": values["trade_contract_size"],
            "tick_size": tick_size,
            "tick_value": tick_value,
            "tick_value_profit": profit_tick,
            "tick_value_loss": loss_tick,
            "tick_value_asymmetry": tick_value_asymmetry,
            "broker_tick_value_per_price_unit": value_per_price_unit,
            "price_precision": {
                "digits": int(digits) if digits is not None and digits >= 0 and digits.is_integer() else None,
                "point": point,
                "tick_to_point_ratio": (
                    tick_size / point if tick_size is not None and tick_size > 0
                    and point is not None and point > 0 else None
                ),
            },
            "volume_constraints": volume_constraints,
            "stop_distance_price_units": stop_distance,
            "freeze_distance_price_units": freeze_distance,
            "currencies": {
                "base": spec.get("currency_base"),
                "profit": spec.get("currency_profit"),
                "margin": spec.get("currency_margin"),
            },
            "broker_trading_sessions": spec.get("broker_trading_sessions"),
            "broker_session_timezone_verified": spec.get("broker_session_timezone_verified") is True,
            "broker_holiday_calendar_verified": spec.get("broker_holiday_calendar_verified") is True,
            "swap": {
                "long": _number(spec.get("swap_long")),
                "short": _number(spec.get("swap_short")),
                "mode": spec.get("swap_mode"),
                "unit_interpreted": False,
            },
            "start_time": spec.get("start_time"),
            "expiration_time": spec.get("expiration_time"),
            "validation_errors": errors,
            "unknown_fields": list(dict.fromkeys(unknowns)),
            "no_config_fallback_used": True,
            "execution_authorization_emitted": False,
            "position_size_emitted": False,
        }
        return [ConceptReading(
            concept="instrument_economics",
            timeframe="SPEC",
            domain=EvidenceDomain.EXECUTION_QUALITY,
            applicable=verified and not errors,
            applicability_reason=(
                "A source-attested broker symbol specification is available for descriptive contract analysis."
                if verified else
                "A broker-verified instrument specification is unavailable; configured fallback values are not substituted."
            ),
            observation=(
                f"{symbol}: broker contract specification state={state}; "
                "contract geometry and broker constraints are descriptive evidence, not execution permission."
            ),
            implications=(),
            facts=facts,
            source_bar_epoch=None,
            reference_close=reference,
            reference_timeframe=reference_tf,
            unknowns=tuple(dict.fromkeys(unknowns)),
            data_basis="broker_symbol_specification",
            independence_group=f"broker_symbol_spec:{source}:{str(symbol or '').upper()}",
            relevance_horizon_seconds=900.0,
        )]

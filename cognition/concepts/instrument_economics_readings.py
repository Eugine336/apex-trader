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
    data_gated_methods = {
        "carry": (
            "verified direction-specific broker swap/financing schedule, rollover multipliers, "
            "and instrument/account-currency conversion"
        ),
    }
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

        reported_values = {key: _number(spec.get(key)) for key in _NUMERIC_FIELDS}
        # Numeric fields from an unattested response remain forensic input only.
        # They must not enter the Brain as usable contract facts.
        values = (
            reported_values if verified
            else {key: None for key in _NUMERIC_FIELDS}
        )
        unverified_reported_fields = {}
        if not verified:
            for key in (*_NUMERIC_FIELDS, "trade_mode", "trade_calc_mode",
                        "currency_base", "currency_profit", "currency_margin",
                        "swap_long", "swap_short", "swap_mode"):
                if key in spec:
                    unverified_reported_fields[key] = spec.get(key)
            raw_info = spec.get("raw_symbol_info")
            if isinstance(raw_info, dict):
                unverified_reported_fields["raw_symbol_info"] = dict(raw_info)
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

        raw_mode = spec.get("trade_mode") if verified else None
        mode_number = _number(raw_mode)
        trade_mode = (
            _TRADE_MODES.get(int(mode_number))
            if mode_number is not None and mode_number.is_integer() else None
        )
        if trade_mode is None:
            unknowns.append("broker trade_mode and its supported-operation semantics")
        for field in ("currency_base", "currency_profit", "currency_margin"):
            if not str((spec.get(field) if verified else None) or "").strip():
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
            "broker_symbol": spec.get("broker_symbol") if verified else None,
            "description": spec.get("description") if verified else None,
            "path": spec.get("path") if verified else None,
            "trade_mode": raw_mode,
            "trade_mode_interpretation": trade_mode,
            "trade_calc_mode": spec.get("trade_calc_mode") if verified else None,
            "trade_execution_mode": spec.get("trade_exemode") if verified else None,
            "filling_mode": spec.get("filling_mode") if verified else None,
            "order_mode": spec.get("order_mode") if verified else None,
            "expiration_mode": spec.get("expiration_mode") if verified else None,
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
                "base": spec.get("currency_base") if verified else None,
                "profit": spec.get("currency_profit") if verified else None,
                "margin": spec.get("currency_margin") if verified else None,
            },
            "broker_trading_sessions": spec.get("broker_trading_sessions") if verified else None,
            "broker_session_timezone_verified": spec.get("broker_session_timezone_verified") is True,
            "broker_holiday_calendar_verified": spec.get("broker_holiday_calendar_verified") is True,
            "swap": {
                "long": _number(spec.get("swap_long")) if verified else None,
                "short": _number(spec.get("swap_short")) if verified else None,
                "mode": spec.get("swap_mode") if verified else None,
                "unit_interpreted": False,
            },
            "start_time": spec.get("start_time") if verified else None,
            "expiration_time": spec.get("expiration_time") if verified else None,
            "unverified_reported_fields": unverified_reported_fields,
            "validation_errors": errors,
            "unknown_fields": list(dict.fromkeys(unknowns)),
            "no_config_fallback_used": True,
            "execution_authorization_emitted": False,
            "position_size_emitted": False,
        }
        instrument_reading = ConceptReading(
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
        )
        carry_reading = self._carry_reading(
            symbol, spec, verified, reference, reference_tf,
        )
        return [instrument_reading, carry_reading]

    @staticmethod
    def _carry_reading(
        symbol: str,
        spec: dict[str, Any],
        verified: bool,
        reference: float | None,
        reference_tf: str,
    ) -> ConceptReading:
        raw_mode = spec.get("swap_mode_name") if verified else None
        mode = str(raw_mode or "").strip().lower()
        valid_modes = {
            "disabled", "points", "currency_symbol", "currency_margin",
            "currency_deposit", "currency_profit", "interest_current",
            "interest_open", "reopen_current", "reopen_bid",
        }
        unknowns: list[str] = []
        errors: list[str] = []
        if not verified:
            unknowns.append("live broker-verified symbol financing specification")
        elif spec.get("source_timestamp_epoch") is None:
            unknowns.append(
                "provider-originated financing specification timestamp; retrieval time is not a verified source timestamp"
            )
        if verified and mode not in valid_modes:
            unknowns.append(
                "swap calculation mode resolved by the installed MetaTrader5 enum constants"
            )
        if verified:
            unknowns.extend([
                "position-specific cash financing amount using actual volume, contract calculation mode and holding interval",
                "account-currency conversion for broker swap modes denominated outside the deposit currency",
                "funding-rate or borrow-rate source for non-MT5 financing instruments",
            ])

        def number(field: str) -> float | None:
            return _number(spec.get(field)) if verified else None

        long_value, short_value = number("swap_long"), number("swap_short")
        point = number("point")
        if verified and mode in valid_modes - {"disabled"}:
            if long_value is None:
                unknowns.append("broker swap_long value")
            if short_value is None:
                unknowns.append("broker swap_short value")
        if mode in {"points", "reopen_current", "reopen_bid"} and (point is None or point <= 0):
            unknowns.append("positive broker point size for interpreting swap points")

        currency_fields = {
            "currency_symbol": ("currency_base", "symbol base currency"),
            "currency_margin": ("currency_margin", "symbol margin currency"),
            "currency_profit": ("currency_profit", "symbol profit currency"),
            "currency_deposit": ("account_currency", "client deposit currency"),
        }
        currency_field, currency_description = currency_fields.get(mode, (None, None))
        currency_code = (
            str(spec.get(currency_field) or "").strip() or None
            if verified and currency_field in {"currency_base", "currency_margin", "currency_profit"}
            else None
        )
        if mode == "currency_deposit":
            currency_description = "client deposit currency; account currency code not supplied with symbol specification"
        elif currency_field and currency_code is None:
            unknowns.append(f"verified {currency_description} code for swap denomination")

        units = {
            "disabled": "no_swap_charge",
            "points": "points",
            "currency_symbol": "money_in_symbol_base_currency",
            "currency_margin": "money_in_symbol_margin_currency",
            "currency_deposit": "money_in_client_deposit_currency",
            "currency_profit": "money_in_symbol_profit_currency",
            "interest_current": "annual_interest_percent_of_current_instrument_price",
            "interest_open": "annual_interest_percent_of_position_open_price",
            "reopen_current": "points_with_forced_reopen_at_previous_close",
            "reopen_bid": "points_with_forced_reopen_at_new_day_bid",
        }
        year_basis = 360 if mode in {"interest_current", "interest_open"} else None
        schedule_raw = spec.get("swap_schedule") if verified else None
        schedule_raw = schedule_raw if isinstance(schedule_raw, dict) else {}
        schedule = {
            day: _number(schedule_raw.get(day))
            for day in ("sunday", "monday", "tuesday", "wednesday",
                        "thursday", "friday", "saturday")
        }
        rollover_raw = spec.get("swap_rollover3days") if verified else None
        rollover_number = _number(rollover_raw)
        days = ("sunday", "monday", "tuesday", "wednesday",
                "thursday", "friday", "saturday")
        rollover_day = (
            days[int(rollover_number)]
            if rollover_number is not None and rollover_number.is_integer()
            and 0 <= int(rollover_number) < len(days) else None
        )
        if verified and mode not in {"", "disabled"}:
            if rollover_day is None:
                unknowns.append("broker three-day swap rollover weekday")
            if any(value is None for value in schedule.values()):
                unknowns.append("complete broker weekday swap-multiplier schedule")
        for day, value in schedule.items():
            if value is not None and value not in {0.0, 1.0, 3.0}:
                errors.append(f"unsupported broker {day} rollover multiplier: {value}")

        def side_reading(value: float | None) -> dict[str, Any] | None:
            if mode not in valid_modes or not verified:
                return None
            if mode == "disabled":
                return {"charge": "none", "raw_value": value}
            if value is None:
                return None
            if mode in {"points", "reopen_current", "reopen_bid"}:
                return {
                    "raw_value": value,
                    "unit": "points",
                    "price_unit_equivalent": value * point if point is not None and point > 0 else None,
                    "cash_amount": None,
                    "unit_note": "price-unit equivalent only; not a cash financing amount",
                    "forced_reopen": mode in {"reopen_current", "reopen_bid"},
                    "reopen_reference": (
                        "previous_session_close" if mode == "reopen_current"
                        else "new_day_bid" if mode == "reopen_bid" else None
                    ),
                }
            if mode in {"interest_current", "interest_open"}:
                return {
                    "annual_interest_percent": value,
                    "daily_rate_fraction_360_day_basis": value / 100.0 / 360.0,
                    "year_basis_days": 360,
                    "price_basis": (
                        "current_instrument_price_at_rollover"
                        if mode == "interest_current" else "position_open_price"
                    ),
                    "cash_charge_calculated": False,
                }
            return {
                "raw_value": value,
                "unit": units[mode],
                "currency_code": currency_code,
                "currency_label": currency_description,
                "cash_charge_calculated": False,
            }

        if not verified:
            state = "unavailable_or_unverified"
        elif errors:
            state = "broker_swap_schedule_inconsistent"
        elif unknowns:
            state = "verified_but_incomplete"
        else:
            state = "verified_carry_terms"
        facts = {
            "status": state,
            "source": spec.get("source") if verified else None,
            "source_verified": verified,
            "swap_mode": spec.get("swap_mode") if verified else None,
            "swap_mode_name": mode or None,
            "swap_units": units.get(mode),
            "long": side_reading(long_value),
            "short": side_reading(short_value),
            "rollover": {
                "triple_swap_weekday_raw": rollover_raw,
                "triple_swap_weekday": rollover_day,
                "daily_multiplier": schedule,
                "documented_multiplier_values": [0.0, 1.0, 3.0],
            },
            "interest_year_basis_days": year_basis,
            "coverage_status": "PARTIAL",
            "unimplemented_components": [
                "position-specific cash financing amount after broker contract volume and calculation mode are applied",
                "account-currency conversion for swaps denominated outside the account deposit currency",
                "holding-period cash projection across the broker rollover calendar",
                "funding-rate and borrow-rate feeds for instruments without MT5 symbol swap fields",
            ],
            "cash_carry_cost_calculated": False,
            "position_context_required_for_cash_cost": [
                "position volume and contract calculation mode",
                "position opening price and holding interval where required",
                "broker conversion rates and account deposit currency where required",
            ],
            "forced_reopen_at_session_end": mode in {"reopen_current", "reopen_bid"},
            "validation_errors": errors,
            "unknown_fields": list(dict.fromkeys(unknowns)),
            "no_swap_rate_fallback_used": True,
            "directional_decision_emitted": False,
            "execution_authorization_emitted": False,
        }
        return ConceptReading(
            concept="carry",
            timeframe="SPEC",
            domain=EvidenceDomain.EXECUTION_QUALITY,
            applicable=verified and mode in valid_modes and not errors,
            applicability_reason=(
                "Broker financing terms have a recognized calculation mode and are available for descriptive carry analysis."
                if verified and mode in valid_modes else
                "Broker swap mode or financing specification is unavailable or unresolved; no carry rate is assumed."
            ),
            observation=(
                f"{symbol}: broker carry mode={mode or 'unknown'}; "
                "side-specific financing is interpreted only in the broker-declared unit and is not converted into a trade decision."
            ),
            implications=(),
            facts=facts,
            source_bar_epoch=None,
            reference_close=reference,
            reference_timeframe=reference_tf,
            unknowns=tuple(dict.fromkeys(unknowns)),
            data_basis="broker_symbol_specification",
            independence_group=f"broker_symbol_spec:{str(spec.get('source') or 'unavailable')}:{str(symbol or '').upper()}",
            relevance_horizon_seconds=900.0,
        )

"""Broker-grounded execution-cost components for the native Apex Brain.

This module is descriptive. It never creates directional signals or execution
authority. A total is verified only when every round-trip cost component is
supported by a broker quote/specification or an explicitly verified model.
Entry-only telemetry is not silently doubled into round-trip slippage.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import time
from typing import Any


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _finite_nonnegative(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0.0 or (positive and number <= 0.0):
        return None
    return number


def _money_per_price(spec: Any) -> tuple[float | None, str]:
    if not isinstance(spec, dict):
        return None, "broker symbol specification unavailable"
    if spec.get("source_verified") is not True or spec.get("specification_verified") is not True:
        return None, "broker symbol specification is not source-attested"
    tick_size = _finite_nonnegative(spec.get("trade_tick_size"), positive=True)
    tick_value = _finite_nonnegative(spec.get("trade_tick_value"), positive=True)
    if tick_value is None:
        profit = _finite_nonnegative(spec.get("trade_tick_value_profit"), positive=True)
        loss = _finite_nonnegative(spec.get("trade_tick_value_loss"), positive=True)
        if profit is not None and loss is not None:
            tick_value = max(profit, loss)
    if tick_size is None or tick_value is None:
        return None, "verified broker tick size/value conversion unavailable"
    value = tick_value / tick_size
    if not math.isfinite(value) or value <= 0.0:
        return None, "broker money-per-price conversion is invalid"
    return value, ""


def build_execution_cost_reading(
    symbol: str,
    quote: Any,
    symbol_spec: Any,
    *,
    commission_per_lot_round_trip: Any = None,
    commission_model_verified: bool = False,
    financing_per_lot_round_trip: Any = None,
    financing_model_verified: bool = False,
    execution_observations: Any = None,
    now_epoch: float | None = None,
    max_quote_age_seconds: float = 5.0,
    min_round_trip_slippage_samples: int = 20,
) -> dict[str, Any]:
    """Build a neutral cost observation and preserve missing values as unknown.

    The current runtime monitor records entry attempts only. Those are not enough
    to infer round-trip slippage. A full total remains unverified until a producer
    attests paired entry-and-exit observations.
    """
    clean_symbol = str(symbol or "").strip().upper()
    try:
        now = float(now_epoch) if now_epoch is not None else time.time()
        if not math.isfinite(now) or now <= 0.0:
            now = time.time()
    except (TypeError, ValueError, OverflowError):
        now = time.time()
    try:
        quote_limit = max(0.0, float(max_quote_age_seconds))
    except (TypeError, ValueError, OverflowError):
        quote_limit = 5.0
    try:
        slippage_sample_floor = max(1, int(min_round_trip_slippage_samples))
    except (TypeError, ValueError, OverflowError):
        slippage_sample_floor = 20

    unknowns: list[str] = []
    quote_facts = None
    source_epoch = None
    quote_valid = False
    bid = ask = spread = quote_age = None
    quote_time = _get(quote, "time")
    if isinstance(quote_time, datetime) and quote_time.tzinfo is not None and quote_time.utcoffset() is not None:
        try:
            candidate_epoch = quote_time.astimezone(timezone.utc).timestamp()
            candidate_age = now - candidate_epoch
            candidate_bid = _finite_nonnegative(_get(quote, "bid"), positive=True)
            candidate_ask = _finite_nonnegative(_get(quote, "ask"), positive=True)
            quote_symbol = str(_get(quote, "symbol", "") or "").strip().upper()
            quote_kind = str(_get(quote, "quote_kind", "unknown") or "unknown").strip().lower()
            source_attested = _get(quote, "source_timestamp_verified", False) is True
            if (
                clean_symbol and quote_symbol == clean_symbol and quote_kind == "bid_ask"
                and source_attested and candidate_epoch > 0.0
                and math.isfinite(candidate_age) and 0.0 <= candidate_age <= quote_limit
                and candidate_bid is not None and candidate_ask is not None
                and candidate_ask > candidate_bid
            ):
                bid, ask = candidate_bid, candidate_ask
                spread = ask - bid
                source_epoch = candidate_epoch
                quote_age = candidate_age
                quote_valid = True
                quote_facts = {
                    "symbol": clean_symbol,
                    "quote_kind": "bid_ask",
                    "bid": bid,
                    "ask": ask,
                    "spread": spread,
                    "source_timestamp_epoch": source_epoch,
                    "source_timestamp_verified": True,
                }
        except (TypeError, ValueError, OverflowError, OSError):
            quote_valid = False
    if not quote_valid:
        unknowns.append("fresh source-verified bid/ask quote with matching symbol")

    money_per_price, spec_error = _money_per_price(symbol_spec)
    if spec_error:
        unknowns.append(spec_error)
    spread_cost = spread * money_per_price if quote_valid and spread is not None and money_per_price is not None else None
    if spread_cost is None:
        unknowns.append("spread cost converted into account currency per lot")

    commission = None
    if commission_model_verified is True:
        commission = _finite_nonnegative(commission_per_lot_round_trip)
        if commission is None:
            unknowns.append("valid verified round-trip commission amount")
    else:
        unknowns.append("explicitly verified round-trip commission model")

    financing = None
    financing_table = financing_per_lot_round_trip if isinstance(financing_per_lot_round_trip, dict) else {}
    financing_key = next(
        (key for key in financing_table if str(key or "").strip().upper() == clean_symbol),
        None,
    )
    if financing_model_verified is True and financing_key is not None:
        financing = _finite_nonnegative(financing_table[financing_key])
        if financing is None:
            unknowns.append("valid verified symbol-specific round-trip financing amount")
    else:
        unknowns.append("verified symbol-specific round-trip financing model")

    observations = execution_observations if isinstance(execution_observations, dict) else {}
    entry_attempts = _finite_nonnegative(observations.get("market_entry_attempt_count"))
    fill_ratio = _finite_nonnegative(observations.get("market_entry_fill_success_ratio"))
    if fill_ratio is not None and fill_ratio > 1.0:
        fill_ratio = None
    entry_slippage_avg = _finite_nonnegative(observations.get("observed_entry_abs_slippage_avg_pips"))
    entry_slippage_samples = _finite_nonnegative(observations.get("observed_entry_slippage_sample_count"))
    submit_latency_avg = _finite_nonnegative(observations.get("observed_submit_response_latency_avg_ms"))
    latency_samples = _finite_nonnegative(observations.get("observed_submit_response_latency_sample_count"))
    fill_min_samples = _finite_nonnegative(observations.get("market_entry_fill_probability_min_samples"), positive=True)
    fill_probability_verified = bool(
        observations.get("market_entry_fill_rate_sample_floor_met") is True
        and entry_attempts is not None and fill_min_samples is not None
        and entry_attempts >= fill_min_samples
    )

    round_trip_slippage = None
    round_trip_samples = _finite_nonnegative(observations.get("observed_round_trip_slippage_sample_count"))
    if (
        observations.get("round_trip_slippage_verified") is True
        and round_trip_samples is not None
        and round_trip_samples >= slippage_sample_floor
        and money_per_price is not None
    ):
        rt_pips = _finite_nonnegative(observations.get("observed_round_trip_abs_slippage_avg_pips"))
        pip_size = _finite_nonnegative(observations.get("pip_size"), positive=True)
        if rt_pips is not None and pip_size is not None:
            round_trip_slippage = rt_pips * pip_size * money_per_price
    if round_trip_slippage is None:
        unknowns.append("verified round-trip entry-and-exit slippage sample; entry-only fills are insufficient")

    components = {
        "spread_account_ccy_per_lot": spread_cost,
        "commission_account_ccy_per_lot": commission,
        "financing_account_ccy_per_lot": financing,
        "round_trip_slippage_account_ccy_per_lot": round_trip_slippage,
    }
    cost_complete = all(value is not None for value in components.values())
    total_cost = sum(float(value) for value in components.values()) if cost_complete else None
    unique_unknowns = list(dict.fromkeys(unknowns))
    return {
        "symbol": clean_symbol,
        "quote": quote_facts,
        "source_timestamp_epoch": source_epoch,
        "source_timestamp_verified": quote_valid,
        "quote_age_seconds": quote_age,
        "broker_spec_source": (
            str(symbol_spec.get("source") or "")[:100]
            if isinstance(symbol_spec, dict)
            and symbol_spec.get("source_verified") is True
            and symbol_spec.get("specification_verified") is True else None
        ),
        "money_per_price_account_ccy_per_lot": money_per_price,
        "estimated_spread_account_ccy_per_lot": spread_cost,
        "estimated_commission_account_ccy_per_lot": commission,
        "estimated_financing_account_ccy_per_lot": financing,
        "estimated_round_trip_slippage_account_ccy_per_lot": round_trip_slippage,
        "estimated_total_cost_account_ccy_per_lot": total_cost,
        "cost_components": components,
        "cost_components_complete": cost_complete,
        "cost_unknown_components": unique_unknowns,
        "cost_verified": bool(cost_complete and quote_valid),
        "cost_source": (
            "broker quote + broker contract specification + verified commission, financing and round-trip slippage models"
            if cost_complete else "partial broker execution-cost observation; total cost is unverified"
        ),
        "market_entry_attempt_count": entry_attempts,
        "market_entry_fill_success_ratio": fill_ratio,
        "market_entry_fill_rate_sample_floor_met": fill_probability_verified,
        "market_entry_fill_probability_sample_count": observations.get("market_entry_fill_probability_sample_count"),
        "observed_entry_abs_slippage_avg_pips": entry_slippage_avg,
        "observed_entry_slippage_sample_count": entry_slippage_samples,
        "observed_submit_response_latency_avg_ms": submit_latency_avg,
        "observed_submit_response_latency_sample_count": latency_samples,
        "observed_entry_spread_avg_price": observations.get("observed_entry_spread_avg_price"),
        "observed_entry_spread_sample_count": observations.get("observed_entry_spread_sample_count"),
        "entry_observation_latest_epoch": observations.get("entry_observation_latest_epoch"),
        "entry_observation_scope": observations.get("entry_observation_scope", "market_entry_attempts_only"),
        "directional_decision_emitted": False,
        "execution_authorization_emitted": False,
    }

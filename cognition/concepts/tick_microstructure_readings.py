"""Tick velocity and quote-dynamics readings from broker tick history.

Rates are calculated from the explicit broker query window and raw returned
tick events, not the coalesced in-memory TickStore. Tick flags remain broker
annotations; they are not converted into executed-volume delta.
"""
from __future__ import annotations

import math
from statistics import median
from typing import Any

from cognition.concepts.base import ConceptReading, finite
from cognition.contracts import EvidenceDomain


def _epoch(value: Any, *, unit: str = "seconds") -> float | None:
    if value is None:
        return None
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        timezone_info = getattr(value, "tzinfo", None)
        if timezone_info is None:
            return None
        try:
            if value.utcoffset() is None:
                return None
            return finite(timestamp())
        except (AttributeError, TypeError, ValueError, OverflowError, OSError):
            return None
    number = finite(value)
    if number is None:
        return None
    return number / 1000.0 if unit == "milliseconds" else number


def _quote_microprice(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {"value": None, "mid_price": None, "source": None, "available": False}
    levels = snapshot.get("levels")
    source = str(snapshot.get("source") or "")
    source_symbol = str(snapshot.get("source_symbol") or "")
    verified = (
        snapshot.get("source_verified") is True
        and bool(source)
        and bool(source_symbol)
        and isinstance(levels, (list, tuple))
        and bool(levels)
        and all(
            isinstance(row, dict)
            and row.get("source_verified") is True
            and row.get("source") == source
            and row.get("source_symbol") == source_symbol
            for row in levels
        )
    )
    if not verified:
        return {"value": None, "mid_price": None, "source": None, "available": False}
    bids = []
    asks = []
    for row in levels:
        price = finite(row.get("price"))
        size = finite(row.get("volume"))
        side = str(row.get("side") or "").lower()
        if price is None or price <= 0 or size is None or size < 0:
            continue
        if side == "bid":
            bids.append((price, size))
        elif side == "ask":
            asks.append((price, size))
    if not bids or not asks:
        return {"value": None, "mid_price": None, "source": source, "available": False}
    bid_price, bid_size = max(bids, key=lambda item: item[0])
    ask_price, ask_size = min(asks, key=lambda item: item[0])
    if bid_price >= ask_price or bid_size + ask_size <= 0:
        return {"value": None, "mid_price": None, "source": source, "available": False}
    mid = (bid_price + ask_price) / 2.0
    microprice = (ask_price * bid_size + bid_price * ask_size) / (bid_size + ask_size)
    return {
        "value": microprice,
        "mid_price": mid,
        "minus_mid": microprice - mid,
        "source": source,
        "source_symbol": source_symbol,
        "volume_basis": sorted({
            str(row.get("volume_basis") or "unspecified")
            for row in levels if isinstance(row, dict)
        }),
        "available": True,
        "provider_timestamp_verified": False,
    }


class TickMicrostructureReadings:
    key = "tick_microstructure_readings"
    family = "microstructure"
    covers_methods = ("tick_microstructure",)
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        return self.read_with_context(symbol, bars_by_tf, {})

    def read_with_context(
        self,
        symbol: str,
        bars_by_tf: dict[str, list[dict]],
        context: dict[str, Any],
    ) -> list[ConceptReading]:
        raw = context.get("tick_history") if isinstance(context, dict) else None
        tape = raw if isinstance(raw, dict) else {}
        rows = tape.get("rows")
        rows = rows if isinstance(rows, (list, tuple)) else []
        source = str(tape.get("source") or "")
        source_symbol = str(tape.get("source_symbol") or "")
        source_verified = (
            tape.get("source_verified") is True
            and bool(source)
            and bool(source_symbol)
            and str(tape.get("source_timezone") or "").upper() == "UTC"
        )
        start = finite(tape.get("window_start_epoch"))
        end = finite(tape.get("window_end_epoch"))
        duration = end - start if start is not None and end is not None and end > start else None

        valid: list[dict[str, Any]] = []
        invalid_count = 0
        timestamp_unverified_count = 0
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                invalid_count += 1
                continue
            raw_time = None
            epoch = None
            naive_timestamp_seen = False
            for time_field, time_unit in (
                ("time_msc_datetime", "seconds"),
                ("time", "seconds"),
                ("time_msc", "milliseconds"),
            ):
                candidate = row.get(time_field)
                if candidate is None:
                    continue
                candidate_epoch = _epoch(candidate, unit=time_unit)
                if candidate_epoch is not None:
                    raw_time = candidate
                    epoch = candidate_epoch
                    break
                # Naive datetimes are rejected by _epoch. Retain that fact so
                # the reading reports unverified source time, not zero activity.
                has_timestamp = callable(getattr(candidate, "timestamp", None))
                if has_timestamp and getattr(candidate, "tzinfo", None) is None:
                    naive_timestamp_seen = True
            bid, ask = finite(row.get("bid")), finite(row.get("ask"))
            if epoch is None or bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
                invalid_count += 1
                if epoch is None and naive_timestamp_seen:
                    timestamp_unverified_count += 1
                continue
            if start is not None and epoch < start:
                invalid_count += 1
                continue
            if end is not None and epoch > end:
                invalid_count += 1
                continue
            # MT5's time/time_msc fields are broker-originated UTC timestamps.
            raw_tz = getattr(raw_time, "tzinfo", None)
            utc_aware = False
            if raw_tz is not None:
                try:
                    offset = raw_time.utcoffset()
                    utc_aware = offset is not None and offset.total_seconds() == 0
                except (AttributeError, TypeError, ValueError, OverflowError):
                    utc_aware = False
            timestamp_verified = bool(
                source_verified and (
                    utc_aware
                    or (isinstance(raw_time, (int, float)) and str(tape.get("source_timezone", "")).upper() == "UTC")
                )
            )
            if not timestamp_verified:
                timestamp_unverified_count += 1
            valid.append({
                "epoch": epoch,
                "bid": bid,
                "ask": ask,
                "spread": ask - bid,
                "mid": (bid + ask) / 2.0,
                "aggressor_side": str(row.get("aggressor_side") or "").lower(),
                "original_index": index,
                "timestamp_verified": timestamp_verified,
            })
        valid.sort(key=lambda item: (item["epoch"], item["original_index"]))
        timestamps_verified = bool(valid) and all(
            item["timestamp_verified"] for item in valid
        )
        unknowns: list[str] = []
        if not source_verified:
            unknowns.append("source-verified broker tick history with UTC source timestamps")
        if duration is None:
            unknowns.append("positive, explicit broker tick-history query window")
        if len(valid) < 2:
            unknowns.append("at least two valid timestamped bid/ask tick events for quote-dynamics comparison")
        if invalid_count:
            unknowns.append(f"{invalid_count} invalid or out-of-window tick record(s) were excluded")
        if timestamp_unverified_count:
            unknowns.append("provider-originated timestamp verification for every accepted tick")
        if not rows:
            unknowns.append("broker returned no tick records in the requested window")

        tick_rate = (
            len(valid) / duration
            if duration is not None and timestamps_verified else None
        )
        bid_updates = ask_updates = quote_updates = spread_changes = 0
        spreads = [item["spread"] for item in valid]
        quote_intervals: list[float] = []
        previous = None
        previous_quote_epoch = None
        for item in valid:
            if previous is not None:
                bid_changed = item["bid"] != previous["bid"]
                ask_changed = item["ask"] != previous["ask"]
                if bid_changed:
                    bid_updates += 1
                if ask_changed:
                    ask_updates += 1
                if bid_changed or ask_changed:
                    quote_updates += 1
                    if previous_quote_epoch is not None:
                        quote_intervals.append((item["epoch"] - previous_quote_epoch) * 1000.0)
                    previous_quote_epoch = item["epoch"]
                if item["spread"] != previous["spread"]:
                    spread_changes += 1
            previous = item
        rate_is_measurable = (
            duration is not None and len(valid) >= 2 and timestamps_verified
        )
        quote_rate = quote_updates / duration if rate_is_measurable else None
        bid_rate = bid_updates / duration if rate_is_measurable else None
        ask_rate = ask_updates / duration if rate_is_measurable else None

        spread_first = spreads[0] if spreads else None
        spread_last = spreads[-1] if spreads else None
        spread_delta = (
            spread_last - spread_first
            if spread_first is not None and spread_last is not None else None
        )
        spread_relative_change = (
            spread_delta / spread_first
            if spread_delta is not None and spread_first is not None and spread_first > 0
            else None
        )
        sorted_spreads = sorted(spreads)
        spread_p95 = (
            sorted_spreads[max(0, math.ceil(0.95 * len(sorted_spreads)) - 1)]
            if sorted_spreads else None
        )
        book_microprice = _quote_microprice(
            context.get("market_depth") if isinstance(context, dict) else None
        )
        if not book_microprice["available"]:
            unknowns.append("verified two-sided Level-2 displayed sizes for a size-weighted microprice")
        latest_mid = valid[-1]["mid"] if valid else None
        if book_microprice["available"] and latest_mid is not None:
            book_microprice["minus_latest_tick_mid"] = book_microprice["value"] - latest_mid

        aggressor_counts = {
            "buy_flagged_ticks": sum(item["aggressor_side"] == "buy" for item in valid),
            "sell_flagged_ticks": sum(item["aggressor_side"] == "sell" for item in valid),
            "unclassified_ticks": sum(item["aggressor_side"] not in {"buy", "sell"} for item in valid),
            "basis": str(tape.get("aggressor_side_basis") or "unknown"),
            "these_are_event_counts_not_executed_volume": True,
        }
        latest_epoch = valid[-1]["epoch"] if valid else None
        facts = {
            "status": (
                "unavailable_or_unverified" if not source_verified
                else "verified_tick_tape" if len(valid) >= 2 and duration is not None and timestamps_verified
                else "verified_but_unverified_timestamps"
                if timestamp_unverified_count or (valid and not timestamps_verified)
                else "verified_but_insufficient_ticks"
            ),
            "source": source or None,
            "source_symbol": source_symbol or None,
            "source_verified": source_verified,
            "source_timezone": tape.get("source_timezone"),
            "query_window_seconds": duration,
            "accepted_tick_count": len(valid),
            "invalid_tick_count": invalid_count,
            "tick_rate_hz": tick_rate,
            "quote_update_count": quote_updates,
            "quote_rate_hz": quote_rate,
            "bid_quote_update_count": bid_updates,
            "bid_quote_rate_hz": bid_rate,
            "ask_quote_update_count": ask_updates,
            "ask_quote_rate_hz": ask_rate,
            "spread": {
                "first_price_units": spread_first,
                "last_price_units": spread_last,
                "change_price_units": spread_delta,
                "relative_change": spread_relative_change,
                "minimum_price_units": min(spreads) if spreads else None,
                "median_price_units": median(spreads) if spreads else None,
                "p95_price_units": spread_p95,
                "maximum_price_units": max(spreads) if spreads else None,
                "change_event_count": spread_changes,
            },
            "quote_intervals_milliseconds": {
                "median": median(quote_intervals) if quote_intervals else None,
                "minimum": min(quote_intervals) if quote_intervals else None,
                "maximum": max(quote_intervals) if quote_intervals else None,
            },
            "latest_tick": (
                {
                    "epoch": latest_epoch,
                    "bid": valid[-1]["bid"],
                    "ask": valid[-1]["ask"],
                    "mid": valid[-1]["mid"],
                    "spread": valid[-1]["spread"],
                }
                if valid else None
            ),
            "microprice": book_microprice,
            "broker_aggressor_flag_counts": aggressor_counts,
            "tick_store_coalescing_used": False,
            "rate_basis": "raw_broker_tick_history_count_over_requested_window",
            "source_timestamp_verified": timestamps_verified,
            "no_directional_decision_emitted": True,
            "execution_authorization_emitted": False,
        }
        applicable = (
            source_verified and duration is not None
            and len(valid) >= 2 and timestamps_verified
        )
        if not applicable and not unknowns:
            unknowns.append("adequate raw broker tick tape required for tick microstructure")
        return [ConceptReading(
            concept="tick_microstructure",
            timeframe="TICK",
            domain=EvidenceDomain.EXECUTION_QUALITY,
            applicable=applicable,
            applicability_reason=(
                "Raw broker tick events in a defined UTC window support tick/quote rates and spread-change measurements."
                if applicable else
                "A verified raw broker tick tape with at least two valid timestamped quotes is unavailable."
            ),
            observation=(
                f"{symbol}: {len(valid)} valid broker ticks were observed in "
                f"{duration if duration is not None else 'an unknown-length'} seconds; "
                f"tick rate={tick_rate}, quote update rate={quote_rate}. "
                "Rates are descriptive feed observations, not directional signals."
            ),
            implications=(),
            facts=facts,
            source_bar_epoch=latest_epoch if timestamps_verified else None,
            source_timestamp_verified=timestamps_verified,
            reference_close=None,
            reference_timeframe="",
            unknowns=tuple(dict.fromkeys(unknowns)),
            data_basis="broker_historical_tick_tape",
            independence_group=f"broker_tick_tape:{source or 'unavailable'}:{source_symbol or symbol.upper()}",
            relevance_horizon_seconds=2.0,
        )]

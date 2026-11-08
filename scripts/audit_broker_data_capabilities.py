"""Read-only MT5 capability and provenance audit for the Apex trading host.

This script uses the process environment exactly as supplied by the operator.
It does not load, inspect, print, or modify .env files. It never sends, edits,
or cancels broker orders. Run it on the Windows host where MT5 is installed.

Example:
    python scripts/audit_broker_data_capabilities.py --output data_capability_audit.json

The report intentionally aggregates account/order/deal information and omits
login IDs, credentials, raw position identifiers, and raw broker history rows.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Make top-level repository packages importable when invoked as a script.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

_TIMEFRAME_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400,
}
_ACCOUNT_FIELDS = ("balance", "equity", "margin", "margin_free", "margin_level", "currency")
_TERMINAL_FIELDS = ("connected", "trade_allowed", "tradeapi_disabled", "build", "company", "name")


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _epoch(value: Any) -> float | None:
    stamp = getattr(value, "timestamp", None)
    if callable(stamp):
        try:
            parsed = float(stamp())
            return parsed if math.isfinite(parsed) and parsed > 0 else None
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return _finite(value)


def _frame_quality(frame: Any, expected_symbol: str, timeframe: str) -> dict[str, Any]:
    attrs = getattr(frame, "attrs", {})
    attrs = attrs if isinstance(attrs, dict) else {}
    times: list[float] = []
    if frame is not None and not getattr(frame, "empty", True):
        for raw in frame["time"].tolist():
            value = _epoch(raw)
            if value is not None:
                times.append(value)
    duration = _TIMEFRAME_SECONDS[timeframe]
    deltas = [right - left for left, right in zip(times, times[1:])]
    return {
        "status": "AVAILABLE" if times else "UNAVAILABLE",
        "rows": len(frame) if frame is not None else 0,
        "first_source_epoch": times[0] if times else None,
        "last_source_epoch": times[-1] if times else None,
        "timestamps_parseable": len(times) == (len(frame) if frame is not None else 0),
        "timestamps_strictly_increasing": all(delta > 0 for delta in deltas),
        "duplicate_timestamp_count": sum(delta == 0 for delta in deltas),
        "interval_gap_count": sum(delta > duration for delta in deltas),
        "source_timestamp_verified": attrs.get("source_timestamp_verified") is True,
        "source_timestamp_origin": attrs.get("source_timestamp_origin"),
        "source_symbol_matches": str(attrs.get("source_symbol", "")).strip().upper()
        == expected_symbol.upper(),
        "volume_basis": attrs.get("volume_basis"),
        "bar_fields": list(attrs.get("bar_fields") or []),
    }


def _history_summary(rows: list[dict], time_fields: tuple[str, ...]) -> dict[str, Any]:
    epochs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in time_fields:
            value = _epoch(row.get(field))
            if value is not None:
                epochs.append(value)
                break
    return {
        "status": "AVAILABLE" if rows else "EMPTY",
        "record_count": len(rows),
        "earliest_source_epoch": min(epochs) if epochs else None,
        "latest_source_epoch": max(epochs) if epochs else None,
        "timestamped_record_count": len(epochs),
    }


def _safe_call(fn, *args, **kwargs):
    try:
        return {"value": fn(*args, **kwargs), "error": None}
    except Exception as exc:  # broker adapters must be reported, not hidden
        return {"value": None, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}


def audit_connector(
    connector: Any,
    connector_index: int,
    configured_symbols: list[str],
    timeframes: list[str],
    bar_count: int,
    history_days: int,
    tick_days: int,
    include_ticks: bool,
    include_depth: bool,
) -> dict[str, Any]:
    connected = _safe_call(connector.connect)
    if connected["value"] is not True:
        return {
            "connector_index": connector_index,
            "connection": {"status": "FAILED", "error": connected["error"]},
            "symbols": {},
        }

    try:
        terminal = _safe_call(connector.get_terminal_snapshot)
        account = _safe_call(connector.get_account_snapshot)
        catalog_result = _safe_call(connector.discover_symbols)
        catalog = catalog_result["value"] or []
        by_canonical: dict[str, list[dict]] = {}
        for row in catalog:
            canonical = str(row.get("apex_symbol", "")).strip().upper()
            if canonical:
                by_canonical.setdefault(canonical, []).append(row)

        now = datetime.now(timezone.utc)
        history_start = now - timedelta(days=history_days)
        tick_start = now - timedelta(days=tick_days)
        symbols: dict[str, Any] = {}
        for symbol in configured_symbols:
            canonical = str(symbol).strip().upper()
            candidates = by_canonical.get(canonical, [])
            exact = [row for row in candidates if row.get("broker_symbol_mapping_unique") is True]
            if len(exact) != 1:
                symbols[canonical] = {
                    "status": "UNAVAILABLE" if not candidates else "AMBIGUOUS",
                    "catalogue_candidates": len(candidates),
                    "reason": "no unique broker-native symbol mapping",
                }
                continue

            broker_symbol = str(exact[0].get("symbol") or "")
            row: dict[str, Any] = {
                "status": "PARTIAL",
                "broker_symbol": broker_symbol,
                "catalogue": {
                    key: exact[0].get(key) for key in (
                        "apex_supported", "trade_mode", "can_open_new_positions",
                        "visible_in_market_watch", "selected_in_terminal",
                        "specification_complete", "new_position_eligible",
                    )
                },
                "timeframes": {},
            }

            for timeframe in timeframes:
                result = _safe_call(
                    connector.get_ohlcv, canonical, timeframe, bar_count,
                    include_forming=False,
                )
                if result["error"]:
                    row["timeframes"][timeframe] = {
                        "status": "UNAVAILABLE", "error": result["error"],
                    }
                else:
                    row["timeframes"][timeframe] = _frame_quality(
                        result["value"], canonical, timeframe,
                    )

            spec_result = _safe_call(connector.get_symbol_spec, canonical)
            spec = spec_result["value"] or {}
            row["instrument_specification"] = {
                "status": "AVAILABLE" if spec.get("specification_verified") is True else "UNVERIFIED",
                "source": spec.get("source"),
                "source_verified": spec.get("source_verified") is True,
                "specification_verified": spec.get("specification_verified") is True,
                "source_timestamp_epoch": spec.get("source_timestamp_epoch"),
                "currency_base": spec.get("currency_base"),
                "currency_profit": spec.get("currency_profit"),
                "currency_margin": spec.get("currency_margin"),
                "trade_contract_size": spec.get("trade_contract_size"),
                "trade_tick_size": spec.get("trade_tick_size"),
                "trade_tick_value": spec.get("trade_tick_value"),
                "volume_min": spec.get("volume_min"),
                "volume_max": spec.get("volume_max"),
                "volume_step": spec.get("volume_step"),
                "swap_long": spec.get("swap_long"),
                "swap_short": spec.get("swap_short"),
                "swap_mode": spec.get("swap_mode"),
                "swap_mode_name": spec.get("swap_mode_name"),
                "swap_rollover3days": spec.get("swap_rollover3days"),
                "broker_trading_sessions": spec.get("broker_trading_sessions"),
                "broker_session_timezone_verified": spec.get("broker_session_timezone_verified") is True,
                "broker_holiday_calendar_verified": spec.get("broker_holiday_calendar_verified") is True,
                "swap_schedule": spec.get("swap_schedule"),
                "error": spec_result["error"],
            }

            if include_depth:
                depth_result = _safe_call(connector.get_market_depth, canonical)
                levels = depth_result["value"] or []
                row["market_depth"] = {
                    "status": "AVAILABLE" if levels else "UNAVAILABLE",
                    "level_count": len(levels),
                    "source": levels[0].get("source") if levels else None,
                    "source_symbol": levels[0].get("source_symbol") if levels else None,
                    "source_verified": bool(levels) and all(
                        isinstance(level, dict) and level.get("source_verified") is True
                        for level in levels
                    ),
                    "snapshot_received_epoch": (
                        levels[0].get("snapshot_received_epoch") if levels else None
                    ),
                    "error": depth_result["error"],
                    "note": "A single DOM snapshot is not executed trade volume or a replenishment time series.",
                }

            if include_ticks:
                ticks_result = _safe_call(
                    connector.get_ticks, canonical, tick_start, now,
                )
                ticks = ticks_result["value"]
                attrs = getattr(ticks, "attrs", {}) if ticks is not None else {}
                columns = set(getattr(ticks, "columns", [])) if ticks is not None else set()
                aggressor = (
                    ticks["aggressor_side"].fillna("unknown").astype(str).str.lower().tolist()
                    if ticks is not None and "aggressor_side" in columns else []
                )
                row["historical_ticks"] = {
                    "status": "AVAILABLE" if ticks is not None and len(ticks) else "EMPTY",
                    "row_count": len(ticks) if ticks is not None else 0,
                    "source": attrs.get("source") if isinstance(attrs, dict) else None,
                    "source_verified": (
                        attrs.get("source_verified") is True if isinstance(attrs, dict) else False
                    ),
                    "source_symbol": attrs.get("source_symbol") if isinstance(attrs, dict) else None,
                    "aggressor_side_basis": (
                        attrs.get("aggressor_side_basis") if isinstance(attrs, dict) else None
                    ),
                    "broker_classified_buy_rows": sum(value == "buy" for value in aggressor),
                    "broker_classified_sell_rows": sum(value == "sell" for value in aggressor),
                    "aggressor_unknown_rows": sum(value not in {"buy", "sell"} for value in aggressor),
                    "error": ticks_result["error"],
                    "note": "MT5 tick flags are not assumed to be exchange trade prints or universal aggressor-side volume.",
                }

            symbols[canonical] = row

        # Use the strict snapshot API: get_open_positions() converts MT5 None/errors to [],
        # which would incorrectly report a failed query as a verified empty book.
        positions = _safe_call(connector.get_position_snapshots)
        orders = _safe_call(connector.get_order_history, history_start, now)
        deals = _safe_call(connector.get_deal_history_checked, history_start, now)
        return {
            "connector_index": connector_index,
            "connection": {"status": "CONNECTED", "error": None},
            "terminal": {
                "status": "AVAILABLE" if terminal["value"] else "UNAVAILABLE",
                "fields": {
                    key: terminal["value"].get(key)
                    for key in _TERMINAL_FIELDS
                } if terminal["value"] else {},
                "error": terminal["error"],
            },
            "account": {
                "status": "AVAILABLE" if account["value"] else "UNAVAILABLE",
                "fields": {
                    key: account["value"].get(key)
                    for key in _ACCOUNT_FIELDS
                } if account["value"] else {},
                "error": account["error"],
            },
            "whole_book": {
                "status": "AVAILABLE" if positions["value"] is not None else "UNAVAILABLE",
                "open_position_count": len(positions["value"] or []),
                "error": positions["error"],
            },
            "order_history": {
                **_history_summary(orders["value"] or [], ("time_setup", "time_done", "time")),
                "error": orders["error"],
            },
            "deal_history": {
                **_history_summary(deals["value"] or [], ("time", "time_msc")),
                "error": deals["error"],
            },
            "symbols": symbols,
            "history_window": {
                "start_epoch": history_start.timestamp(),
                "end_epoch": now.timestamp(),
                "tick_start_epoch": tick_start.timestamp(),
                "tick_days": tick_days,
            },
        }
    finally:
        _safe_call(connector.disconnect)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--timeframes", nargs="+", default=["M5", "M15", "H1", "H4", "D1"])
    parser.add_argument("--bars", type=int, default=500)
    parser.add_argument("--history-days", type=int, default=30)
    parser.add_argument("--tick-days", type=int, default=1)
    parser.add_argument("--skip-ticks", action="store_true")
    parser.add_argument("--skip-depth", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.bars < 2 or args.history_days < 1 or args.tick_days < 1:
        parser.error("--bars must be >=2 and history/tick days must be >=1")
    timeframes = [str(value).upper() for value in args.timeframes]
    invalid = sorted(set(timeframes) - set(_TIMEFRAME_SECONDS))
    if invalid or len(set(timeframes)) != len(timeframes):
        parser.error(f"unsupported or duplicate timeframes: {invalid or timeframes}")

    from platforms.platform_manager import PlatformManager

    manager = PlatformManager()
    symbols = list(args.symbols or manager.config.enabled_pairs)
    symbols = list(dict.fromkeys(str(value).strip().upper() for value in symbols if str(value).strip()))
    if not symbols:
        parser.error("no symbols supplied and config.enabled_pairs is empty")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "read-only MT5 broker capability audit",
        "configured_symbols": symbols,
        "timeframes": timeframes,
        "bars_requested": args.bars,
        "history_days": args.history_days,
        "tick_days": args.tick_days,
        "connectors": [],
        "execution_mutations_performed": 0,
        "env_file_accessed": False,
    }
    for index, connector in enumerate(manager.mt5_connectors):
        report["connectors"].append(audit_connector(
            connector, index, symbols, timeframes, args.bars,
            args.history_days, args.tick_days,
            include_ticks=not args.skip_ticks,
            include_depth=not args.skip_depth,
        ))

    report["status"] = (
        "complete" if report["connectors"] and all(
            item.get("connection", {}).get("status") == "CONNECTED"
            for item in report["connectors"]
        ) else "partial"
    )
    rendered = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())

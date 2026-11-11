"""Read-only MT5 timestamp semantics capture for WP-T.

Uses an already-running terminal session. It never submits or modifies orders,
writes terminal settings, changes the host clock, or reads Apex credentials.
Run on each supported Windows host and retain the JSON output as test evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import platform
import time
from typing import Any


def _record(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    asdict = getattr(value, "_asdict", None)
    if callable(asdict):
        return dict(asdict())
    if isinstance(value, dict):
        return dict(value)
    return {"repr": repr(value)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, help="Exact broker-native MT5 symbol")
    parser.add_argument("--window-seconds", type=int, default=60)
    args = parser.parse_args()
    if args.window_seconds < 1 or args.window_seconds > 300:
        parser.error("--window-seconds must be between 1 and 300")

    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise SystemExit(f"MetaTrader5 package unavailable: {exc}") from exc

    from platforms.mt5.clock import query_ntp_sources

    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        if account is None or terminal is None:
            raise RuntimeError("MT5 account_info or terminal_info unavailable")

        # Capture the latest quote bracketed by monotonic and host wall readings.
        mono_before = time.monotonic()
        wall_before = time.time()
        tick = mt5.symbol_info_tick(args.symbol)
        wall_after = time.time()
        mono_after = time.monotonic()

        # The API request contract requires aware UTC datetimes. Preserve the
        # exact bounds so an empty/shifted response can be diagnosed, not guessed.
        request_end = datetime.fromtimestamp(wall_after, tz=timezone.utc)
        request_start = request_end - timedelta(seconds=args.window_seconds)
        history = mt5.copy_ticks_range(
            args.symbol, request_start, request_end,
            getattr(mt5, "COPY_TICKS_ALL", 0),
        )
        history_rows = []
        if history is not None:
            for row in history[-20:]:
                history_rows.append({
                    key: (value.item() if callable(getattr(value, "item", None)) else value)
                    for key, value in _record(row).items()
                })

        witness_before = time.monotonic()
        witnesses = query_ntp_sources()
        witness_after = time.monotonic()

        output = {
            "schema": "apex.mt5_timestamp_semantics_capture.v1",
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "host": {
                "platform": platform.platform(),
                "timezone_name": str(datetime.now().astimezone().tzinfo),
                "wall_epoch_before": wall_before,
                "wall_epoch_after": wall_after,
                "monotonic_before": mono_before,
                "monotonic_after": mono_after,
            },
            "mt5": {
                "package_version": getattr(mt5, "__version__", None),
                "package_author": getattr(mt5, "__author__", None),
                "terminal_version": mt5.version(),
                "terminal_company": getattr(terminal, "company", None),
                "terminal_connected": getattr(terminal, "connected", None),
                "broker_server": getattr(account, "server", None),
                "symbol": args.symbol,
            },
            "latest_tick": _record(tick),
            "history_query": {
                "from_utc": request_start.isoformat(),
                "to_utc": request_end.isoformat(),
                "row_count": 0 if history is None else len(history),
                "last_rows": history_rows,
                "last_error": mt5.last_error() if history is None else None,
            },
            "independent_utc_witnesses": [
                {
                    "utc_epoch": sample.utc_epoch,
                    "monotonic_epoch": sample.monotonic_epoch,
                    "uncertainty_seconds": sample.uncertainty_seconds,
                    "authority": sample.server,
                }
                for sample in witnesses
            ],
            "witness_collection_monotonic": {
                "before": witness_before,
                "after": witness_after,
            },
            "safety": {
                "orders_submitted": False,
                "host_clock_modified": False,
                "credentials_read": False,
                "terminal_settings_modified": False,
            },
        }
        print(json.dumps(output, indent=2, default=str, sort_keys=True))
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

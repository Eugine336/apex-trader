"""Read-only MT5 tick/host clock diagnostic.

This utility does not load dotenv, read credentials, or submit/modify/cancel
orders. It attaches to the currently installed MT5 terminal through the Python
package and compares provider epoch fields with the host UTC clock.
"""

from __future__ import annotations

import argparse
import subprocess
import time
from datetime import datetime, timezone
from typing import Any


def measure_tick_clock(tick: Any, host_epoch: float) -> dict[str, float | None]:
    """Compare MT5 seconds/milliseconds epochs with the host UTC epoch."""
    seconds = float(getattr(tick, "time", 0) or 0)
    milliseconds = float(getattr(tick, "time_msc", 0) or 0)
    seconds_epoch = seconds if seconds > 0 else None
    milliseconds_epoch = milliseconds / 1000.0 if milliseconds > 0 else None
    return {
        "tick_time_epoch": seconds_epoch,
        "tick_time_msc_epoch": milliseconds_epoch,
        "time_fields_delta_seconds": (
            milliseconds_epoch - seconds_epoch
            if milliseconds_epoch is not None and seconds_epoch is not None
            else None
        ),
        "host_skew_seconds": (
            seconds_epoch - host_epoch if seconds_epoch is not None else None
        ),
        "host_skew_msc_seconds": (
            milliseconds_epoch - host_epoch
            if milliseconds_epoch is not None else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols", nargs="*", default=None,
        help="Exact broker symbols to inspect; default: first five visible symbols",
    )
    parser.add_argument(
        "--terminal-path", default=None,
        help="Exact terminal64.exe path; omit to use MT5 automatic discovery",
    )
    args = parser.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MetaTrader5 Python package is unavailable in this interpreter.")
        return 2

    initialized = (
        mt5.initialize(args.terminal_path)
        if args.terminal_path else mt5.initialize()
    )
    if not initialized:
        print(f"MT5 initialize failed: {mt5.last_error()}")
        return 2

    try:
        host_now = datetime.now(timezone.utc)
        host_epoch = time.time()
        print(f"host_utc={host_now.isoformat()}")
        print(f"host_epoch_time_time={host_epoch:.3f}")
        print(f"host_local_timezone={datetime.now().astimezone().tzinfo}")
        try:
            status = subprocess.run(
                ["w32tm", "/query", "/status"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            print("windows_time_service_status:")
            print((status.stdout or status.stderr or "no status output").strip())
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"windows_time_service_status_unavailable={type(exc).__name__}")

        account = mt5.account_info()
        terminal = mt5.terminal_info()
        print(f"mt5_account_server={getattr(account, 'server', None)}")
        print(f"mt5_terminal_company={getattr(terminal, 'company', None)}")
        print(f"mt5_terminal_build={getattr(terminal, 'build', None)}")

        symbols = list(args.symbols or [])
        if not symbols:
            visible = mt5.symbols_get()
            if visible is None:
                print(f"symbols_get failed: {mt5.last_error()}")
                return 2
            for item in visible:
                info = mt5.symbol_info(item.name)
                if info is not None and bool(getattr(info, "visible", False)):
                    symbols.append(item.name)
                if len(symbols) >= 5:
                    break

        if not symbols:
            print("No visible broker symbols found; pass exact symbols with --symbols.")
            return 2

        for symbol in symbols:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                print(f"{symbol}: no tick ({mt5.last_error()})")
                continue
            measurement = measure_tick_clock(tick, host_epoch)
            print(
                f"{symbol}: time={measurement['tick_time_epoch']} "
                f"time_msc={measurement['tick_time_msc_epoch']} "
                f"fields_delta={measurement['time_fields_delta_seconds']} "
                f"host_skew={measurement['host_skew_seconds']} "
                f"host_skew_msc={measurement['host_skew_msc_seconds']}"
            )
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

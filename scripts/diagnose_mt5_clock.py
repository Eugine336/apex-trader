"""Read-only MT5 broker-clock synchronization diagnostic.

Uses advancing terminal ticks and independent network UTC observations. It does
not use the workstation wall clock as a broker-time substitute and never sends
or modifies broker orders.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
import sys
from typing import Any, Iterable

# Running a file under scripts/ puts that directory, not the repository root,
# at sys.path[0]. Add the project root so package imports work from a venv.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from platforms.mt5.clock import NTPClockSample, estimate_mt5_server_clock, query_ntp_sources


def measure_tick_clock(
    tick: Any,
    server: str,
    observed_monotonic: float,
    ntp_samples: Iterable[NTPClockSample],
) -> dict[str, float | str | None]:
    """Estimate server offset and preserve raw tick timestamp consistency."""
    seconds = float(getattr(tick, "time", 0) or 0)
    milliseconds = float(getattr(tick, "time_msc", 0) or 0)
    tick_epoch = milliseconds / 1000.0 if milliseconds > 0 else seconds
    state = estimate_mt5_server_clock(
        server, tick_epoch, observed_monotonic,
        ntp_samples=ntp_samples,
        tick_precision_seconds=0.001 if milliseconds > 0 else 1.0,
        tick_transport_uncertainty_seconds=1.0,
    )
    return {
        "tick_time_epoch": seconds if seconds > 0 else None,
        "tick_time_msc_epoch": milliseconds / 1000.0 if milliseconds > 0 else None,
        "time_fields_delta_seconds": (
            milliseconds / 1000.0 - seconds
            if milliseconds > 0 and seconds > 0 else None
        ),
        "server": state.server if state else None,
        "offset_seconds": state.offset_seconds if state else None,
        "uncertainty_seconds": state.uncertainty_seconds if state else None,
        "time_sources": ",".join(state.sources) if state else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols", nargs="*", default=None,
        help="Exact broker symbols to inspect; default: first five visible symbols",
    )
    parser.add_argument(
        "--terminal-path", default=os.environ.get("MT5_TERMINAL_PATH") or None,
        help="Exact terminal64.exe path; defaults to MT5_TERMINAL_PATH",
    )
    args = parser.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MetaTrader5 Python package is unavailable in this interpreter.")
        return 2

    if args.terminal_path and not Path(args.terminal_path).is_file():
        print("Configured MT5 terminal executable does not exist.")
        return 2
    initialized = mt5.initialize(args.terminal_path) if args.terminal_path else mt5.initialize()
    if not initialized:
        print(f"MT5 initialize failed: {mt5.last_error()}")
        return 2

    try:
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        server = str(getattr(account, "server", "") or "").strip()
        print(f"mt5_account_server={server or 'unavailable'}")
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
        if not server:
            print("Broker server identity unavailable; synchronization cannot be verified.")
            return 2

        samples = query_ntp_sources()
        print(f"independent_network_time_sources={len(samples)}")
        for symbol in symbols:
            first = mt5.symbol_info_tick(symbol)
            first_mono = time.monotonic()
            if first is None:
                print(f"{symbol}: no tick ({mt5.last_error()})")
                continue
            time.sleep(1.05)
            tick = mt5.symbol_info_tick(symbol)
            observed_mono = time.monotonic()
            if tick is None:
                print(f"{symbol}: no second tick observation ({mt5.last_error()})")
                continue
            first_ms = float(getattr(first, "time_msc", 0) or getattr(first, "time", 0) * 1000)
            second_ms = float(getattr(tick, "time_msc", 0) or getattr(tick, "time", 0) * 1000)
            elapsed = observed_mono - first_mono
            advance = (second_ms - first_ms) / 1000.0
            if advance <= 0 or elapsed <= 0 or elapsed > 10 or abs(advance - elapsed) > max(2.0, elapsed * 0.5):
                print(f"{symbol}: tick stream did not advance consistently (elapsed={elapsed:.3f}s advance={advance:.3f}s)")
                continue
            measured = measure_tick_clock(tick, server, observed_mono, samples)
            print(
                f"{symbol}: server={measured['server']} "
                f"offset={measured['offset_seconds']}s "
                f"uncertainty={measured['uncertainty_seconds']}s "
                f"sources={measured['time_sources']} "
                f"time={measured['tick_time_epoch']} "
                f"time_msc={measured['tick_time_msc_epoch']}"
            )
            if measured["offset_seconds"] is not None:
                print("broker_clock_sync=accepted")
                return 0
        print("broker_clock_sync=unavailable; no advancing tick plus agreeing network time sources")
        return 1
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

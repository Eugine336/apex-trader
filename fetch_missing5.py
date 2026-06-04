"""
APEX TRADER — fetch_missing5.py
=================================
Fetches only the 5 symbols that were skipped:
  XAUUSD, XAGUSD, US100, GER40, FRA40

Tries multiple broker name variants automatically.
Run from project root:
  python fetch_missing5.py
"""

import time
import pandas as pd
from pathlib import Path
from datetime import datetime

try:
    import MetaTrader5 as mt5
except ImportError:
    raise SystemExit("MetaTrader5 not installed.")

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

TIMEFRAMES = {
    "H4":  (mt5.TIMEFRAME_H4,  30_000),
    "H1":  (mt5.TIMEFRAME_H1,  80_000),
    "M15": (mt5.TIMEFRAME_M15, 50_000),
    "M5":  (mt5.TIMEFRAME_M5,  50_000),
    "M1":  (mt5.TIMEFRAME_M1,  50_000),
}

# Canonical name → broker name candidates to try in order
MISSING = {
    "XAUUSD": ["XAUUSD", "GOLD", "XAUUSDm", "XAUUSD."],
    "XAGUSD": ["XAGUSD", "SILVER", "XAGUSDm", "XAGUSD."],
    "US100":  ["US100", "NAS100", "USTEC", "US100m", "NAS100m"],
    "GER40":  ["GER40", "DE40", "DAX40", "GER40m", "DAX"],
    "FRA40":  ["FRA40", "CAC40", "FRA40m", "CACC.NAS", "CAC40m"],
}


def resolve_symbol(candidates: list[str]) -> str | None:
    """Return first candidate that exists on the broker."""
    all_symbols = {s.name for s in (mt5.symbols_get() or [])}
    for c in candidates:
        if c in all_symbols:
            return c
    return None


def fetch(canonical: str, broker_symbol: str, tf_name: str,
          tf_const: int, n_bars: int) -> bool:
    out = DATA_DIR / f"{canonical}_{tf_name}.csv"

    # Retry ladder
    candidates = [n_bars]
    step = n_bars
    while step > 5000:
        step = step // 2
        candidates.append(step)
    candidates += [5000, 1000]
    seen = set()
    retry_counts = [c for c in candidates if not (c in seen or seen.add(c))]

    rates = None
    used_count = n_bars
    for attempt in retry_counts:
        rates = mt5.copy_rates_from_pos(broker_symbol, tf_const, 0, attempt)
        if rates is not None and len(rates) > 0:
            used_count = attempt
            break
        time.sleep(0.1)

    if rates is None or len(rates) == 0:
        print(f"    ✗ {tf_name}: no data ({mt5.last_error()})")
        return False

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    cols = ["time", "open", "high", "low", "close"]
    if "tick_volume" in df.columns:
        df = df.rename(columns={"tick_volume": "volume"})
        cols.append("volume")
    else:
        df["volume"] = 1.0
        cols.append("volume")
    df = df[cols]
    df.to_csv(out, index=False)

    capped = f" [capped at {used_count:,}]" if used_count < n_bars else ""
    start  = df["time"].min().strftime("%Y-%m-%d")
    broker_note = f" (broker: {broker_symbol})" if broker_symbol != canonical else ""
    print(f"    ✓ {tf_name}: {len(df):,} bars  from {start}{capped}{broker_note}")
    return True


def main():
    print("=" * 55)
    print("  Fetching 5 missing symbols")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    if not mt5.initialize():
        raise SystemExit(f"MT5 init failed: {mt5.last_error()}")

    account = mt5.account_info()
    if account:
        print(f"  Connected: {account.login} | {account.server}\n")

    for canonical, candidates in MISSING.items():
        broker_symbol = resolve_symbol(candidates)
        if broker_symbol is None:
            print(f"  ✗ {canonical} — not found on broker")
            print(f"    Tried: {candidates}")
            continue

        note = f" → {broker_symbol}" if broker_symbol != canonical else ""
        print(f"  {canonical}{note}")
        mt5.symbol_select(broker_symbol, True)
        time.sleep(0.05)

        for tf_name, (tf_const, n_bars) in TIMEFRAMES.items():
            fetch(canonical, broker_symbol, tf_name, tf_const, n_bars)
            time.sleep(0.02)

        time.sleep(0.1)

    mt5.shutdown()

    csv_files  = list(DATA_DIR.glob("*.csv"))
    total_size = sum(f.stat().st_size for f in csv_files) / (1024 * 1024)
    print(f"\n  Done. Total data folder: {len(csv_files)} files, {total_size:.1f} MB")
    print("=" * 55)


if __name__ == "__main__":
    main()

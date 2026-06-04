"""
APEX TRADER — fetch_csv.py
===========================
Fetches historical OHLCV data from MT5 for all 65 instruments
across all 5 timeframes and saves to data/.

Timeframes fetched:
  H4  — 3,000 bars  (~2 years)
  H1  — 6,000 bars  (~2.5 years)
  M15 — 20,000 bars (~208 days)
  M5  — 50,000 bars (~174 days)
  M1  — 50,000 bars (~35 days)

Synthetics (Deriv) are skipped — MT5 doesn't carry them.
ESP35 is skipped — not available on MetaQuotes virtual broker.

Run from project root:
  python fetch_csv.py

Output:
  data/EURUSD_H4.csv
  data/EURUSD_H1.csv
  ... (one file per instrument per timeframe)
"""

import os
import sys
import time
import pandas as pd
from pathlib import Path
from datetime import datetime

try:
    import MetaTrader5 as mt5
except ImportError:
    raise SystemExit("MetaTrader5 package not installed. Run: pip install MetaTrader5")


# ── Output directory ──────────────────────────────────────────────────────────

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


# ── Timeframes ────────────────────────────────────────────────────────────────

TIMEFRAMES = {
    "H4":  (mt5.TIMEFRAME_H4,  3_000),
    "H1":  (mt5.TIMEFRAME_H1,  6_000),
    "M15": (mt5.TIMEFRAME_M15, 20_000),
    "M5":  (mt5.TIMEFRAME_M5,  50_000),
    "M1":  (mt5.TIMEFRAME_M1,  50_000),
}


# ── Instrument list ───────────────────────────────────────────────────────────
# All MT5-available instruments (synthetics and ESP35 excluded)

MT5_INSTRUMENTS = [
    # Forex majors (7)
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "AUDUSD", "NZDUSD", "USDCAD",

    # Forex crosses (21)
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY",
    "CADJPY", "CHFJPY", "EURCHF", "EURAUD", "EURCAD",
    "EURNZD", "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD",
    "AUDCAD", "AUDCHF", "AUDNZD", "NZDCAD", "NZDCHF",
    "CADCHF",

    # Commodities (4)
    "XAUUSD", "XAGUSD", "XBRUSD", "XTIUSD",

    # Indices (9 — ESP35 excluded, not on MetaQuotes virtual)
    "US100", "US30", "US500", "GER40", "UK100",
    "JP225", "AUS200", "FRA40", "HK50",

    # Crypto (8)
    "BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD",
    "BNBUSD", "SOLUSD", "ADAUSD", "DOTUSD",
]

# Synthetics are Deriv-only — MT5 cannot fetch them
DERIV_ONLY = [
    "V10_1S", "V25_1S", "V50_1S", "V75_1S", "V100_1S",
    "BOOM500", "BOOM1000", "CRASH500", "CRASH1000", "STPIDX",
    "RNGBULL", "RNGBEAR", "JD10", "JD25", "JD50",
]


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch(symbol: str, tf_name: str, tf_const: int, n_bars: int) -> bool:
    """
    Fetch n_bars of tf_const data for symbol from MT5.
    Returns True on success, False on failure.
    """
    out = DATA_DIR / f"{symbol}_{tf_name}.csv"

    rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, n_bars)

    if rates is None or len(rates) == 0:
        err = mt5.last_error()
        print(f"    ✗ {tf_name}: no data ({err})")
        return False

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)

    # Keep OHLCV — include tick_volume as volume proxy
    cols = ["time", "open", "high", "low", "close"]
    if "tick_volume" in df.columns:
        df = df.rename(columns={"tick_volume": "volume"})
        cols.append("volume")
    elif "real_volume" in df.columns:
        df = df.rename(columns={"real_volume": "volume"})
        cols.append("volume")
    else:
        df["volume"] = 1.0
        cols.append("volume")

    df = df[cols]
    df.to_csv(out, index=False)
    print(f"    ✓ {tf_name}: {len(df):,} bars → {out}")
    return True


def fetch_symbol(symbol: str) -> dict:
    """Fetch all timeframes for one symbol. Returns stats."""
    # Ensure symbol is selected in Market Watch
    mt5.symbol_select(symbol, True)
    time.sleep(0.05)  # small pause to allow MT5 to load symbol data

    info = mt5.symbol_info(symbol)
    if info is None:
        print(f"  ✗ {symbol} — not found on broker, skipping")
        return {"symbol": symbol, "skipped": True}

    if info.trade_mode == 0:
        print(f"  ⚠ {symbol} — disabled on broker (trade_mode=0), fetching anyway")

    print(f"  {symbol}")
    results = {}
    for tf_name, (tf_const, n_bars) in TIMEFRAMES.items():
        ok = fetch(symbol, tf_name, tf_const, n_bars)
        results[tf_name] = ok
        time.sleep(0.02)

    return {"symbol": symbol, "skipped": False, "results": results}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  APEX TRADER — Historical Data Fetch")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Initialise MT5
    if not mt5.initialize():
        raise SystemExit(f"MT5 init failed: {mt5.last_error()}")

    account = mt5.account_info()
    if account:
        print(f"\nConnected: {account.login} | {account.server} | ${account.balance:.2f}\n")

    total      = len(MT5_INSTRUMENTS)
    skipped    = []
    failed     = []
    successful = []

    for i, symbol in enumerate(MT5_INSTRUMENTS, 1):
        print(f"[{i:>2}/{total}] Fetching {symbol}...")
        stat = fetch_symbol(symbol)

        if stat.get("skipped"):
            skipped.append(symbol)
        else:
            tf_results = stat.get("results", {})
            any_fail = any(not v for v in tf_results.values())
            if any_fail:
                failed.append(symbol)
            else:
                successful.append(symbol)

        # Small delay between symbols to avoid hammering MT5
        time.sleep(0.1)

    mt5.shutdown()

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  FETCH COMPLETE")
    print("=" * 60)
    print(f"  ✓ Successful : {len(successful)}")
    print(f"  ✗ Failed     : {len(failed)}")
    print(f"  ⊘ Skipped    : {len(skipped)}")

    if failed:
        print(f"\n  Failed symbols: {', '.join(failed)}")
    if skipped:
        print(f"\n  Skipped (not on broker): {', '.join(skipped)}")

    print(f"\n  Deriv synthetics (fetch separately via Deriv API):")
    print(f"  {', '.join(DERIV_ONLY)}")

    # Count files written
    csv_files = list(DATA_DIR.glob("*.csv"))
    total_size = sum(f.stat().st_size for f in csv_files) / (1024 * 1024)
    print(f"\n  Total CSV files: {len(csv_files)}")
    print(f"  Total data size: {total_size:.1f} MB")
    print("=" * 60)


if __name__ == "__main__":
    main()

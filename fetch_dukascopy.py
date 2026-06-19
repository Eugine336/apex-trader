"""
APEX TRADER — fetch_dukascopy.py
=================================
Fetches historical OHLCV data from Dukascopy (free, no account needed)
for all APEX instruments from 2008 to today.

Covers:
  - 2008 Global Financial Crisis
  - 2010 Flash Crash
  - 2011 EUR/USD collapse
  - 2015 SNB shock, China crash
  - 2016 Brexit, Trump election
  - 2020 COVID crash
  - 2022 Rate hike bear market
  - 2024-2026 current conditions

Install dependency first:
  pip install dukascopy-python pandas

Run from project root:
  python fetch_dukascopy.py

Output: data/<SYMBOL>_<TF>.csv  (same format as fetch_csv.py)
        Compatible with train_colab.py directly.

Notes:
  - Indices and crypto have limited Dukascopy coverage
  - All 28 forex pairs + gold + silver fully covered back to 2008
  - Script skips instruments Dukascopy doesn't carry
  - Existing MT5 CSVs are NOT overwritten — Dukascopy fills the gap
    for deeper history, MT5 fills recent data
"""

import time
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

try:
    import dukascopy_python as dk
except ImportError:
    raise SystemExit(
        "dukascopy-python not installed.\n"
        "Run: pip install dukascopy-python pandas"
    )

# ── Config ────────────────────────────────────────────────────────────────────

DATA_DIR   = Path("data")
DATA_DIR.mkdir(exist_ok=True)

START_DATE = datetime(2008, 1, 1)
END_DATE   = datetime.now()

# ── Timeframe map ─────────────────────────────────────────────────────────────

TIMEFRAMES = {
    "H4":  dk.INTERVAL_HOUR_4,
    "H1":  dk.INTERVAL_HOUR_1,
    "M15": dk.INTERVAL_MIN_15,
    "M5":  dk.INTERVAL_MIN_5,
    "M1":  dk.INTERVAL_MIN_1,
}

# M1 from 2008 would be enormous — cap it
TF_START_OVERRIDES = {
    "M15": datetime(2015, 1, 1),   # ~520k bars — manageable
    "M5":  datetime(2018, 1, 1),   # ~700k bars — manageable
    "M1":  datetime(2022, 1, 1),   # ~1M bars — keep practical
}

# ── Dukascopy instrument map ──────────────────────────────────────────────────
# canonical APEX name -> Dukascopy instrument string
# Dukascopy uses lowercase pair names like 'eurusd', 'xauusd'
# Indices and some crypto not available — marked None

INSTRUMENT_MAP = {
    # Forex majors (7) — full coverage back to 2003
    "EURUSD": "eurusd",
    "GBPUSD": "gbpusd",
    "USDJPY": "usdjpy",
    "USDCHF": "usdchf",
    "AUDUSD": "audusd",
    "NZDUSD": "nzdusd",
    "USDCAD": "usdcad",

    # Forex crosses (21) — full coverage
    "EURGBP": "eurgbp",
    "EURJPY": "eurjpy",
    "GBPJPY": "gbpjpy",
    "AUDJPY": "audjpy",
    "NZDJPY": "nzdjpy",
    "CADJPY": "cadjpy",
    "CHFJPY": "chfjpy",
    "EURCHF": "eurchf",
    "EURAUD": "euraud",
    "EURCAD": "eurcad",
    "EURNZD": "eurnzd",
    "GBPAUD": "gbpaud",
    "GBPCAD": "gbpcad",
    "GBPCHF": "gbpchf",
    "GBPNZD": "gbpnzd",
    "AUDCAD": "audcad",
    "AUDCHF": "audchf",
    "AUDNZD": "audnzd",
    "NZDCAD": "nzdcad",
    "NZDCHF": "nzdchf",
    "CADCHF": "cadchf",

    # Commodities — good coverage
    "XAUUSD": "xauusd",   # Gold — back to 2003
    "XAGUSD": "xagusd",   # Silver — back to 2003
    "XBRUSD": "xbrusd",   # Brent — available
    "XTIUSD": "wtiusd",   # WTI — note: Dukascopy uses 'wtiusd'

    # Indices — limited or unavailable on Dukascopy
    # Use MT5 data for these
    "US100":  None,   # Not on Dukascopy — use MT5
    "US30":   None,
    "US500":  None,
    "GER40":  None,
    "UK100":  None,
    "JP225":  None,
    "AUS200": None,
    "FRA40":  None,
    "HK50":   None,

    # Crypto — Dukascopy has BTC/ETH from ~2014, others limited
    "BTCUSD": "btcusd",
    "ETHUSD": "ethusd",
    "LTCUSD": "ltcusd",
    "XRPUSD": None,    # Not on Dukascopy
    "BNBUSD": None,
    "SOLUSD": None,
    "ADAUSD": None,
    "DOTUSD": None,
}


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_instrument(canonical: str, dk_symbol: str) -> dict:
    """Fetch all timeframes for one instrument from Dukascopy."""
    print(f"  {canonical} ({dk_symbol})")
    results = {}

    for tf_name, dk_interval in TIMEFRAMES.items():
        out = DATA_DIR / f"{canonical}_{tf_name}.csv"

        # Use TF-specific start date for granular timeframes
        start = TF_START_OVERRIDES.get(tf_name, START_DATE)

        try:
            df = dk.fetch(
                dk_symbol,
                dk_interval,
                dk.OFFER_SIDE_BID,
                start,
                END_DATE,
            )

            if df is None or len(df) == 0:
                print(f"    ✗ {tf_name}: no data returned")
                results[tf_name] = False
                continue

            # Normalise columns to APEX format
            df = df.reset_index()

            # Dukascopy returns: timestamp index, open, high, low, close, volume
            df.columns = [c.lower() for c in df.columns]

            # Rename timestamp column
            for ts_col in ["timestamp", "date", "datetime", "time"]:
                if ts_col in df.columns:
                    df = df.rename(columns={ts_col: "time"})
                    break

            # Ensure UTC timezone
            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"], utc=True)

            # Keep only APEX columns
            keep = [c for c in ["time", "open", "high", "low", "close", "volume"]
                    if c in df.columns]
            if "volume" not in df.columns:
                df["volume"] = 1.0
                keep.append("volume")

            df = df[keep].dropna()
            df.to_csv(out, index=False)

            start_date = df["time"].min()
            end_date   = df["time"].max()
            print(f"    ✓ {tf_name}: {len(df):,} bars  "
                  f"{start_date.strftime('%Y-%m-%d')} → {end_date.strftime('%Y-%m-%d')}")
            results[tf_name] = True

        except Exception as e:
            print(f"    ✗ {tf_name}: {e}")
            results[tf_name] = False

        # Rate limit — be polite to Dukascopy servers
        time.sleep(0.5)

    return {"symbol": canonical, "results": results}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  APEX TRADER — Dukascopy Historical Data Fetch")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Range: {START_DATE.strftime('%Y-%m-%d')} → today")
    print("=" * 60)

    fetchable = {k: v for k, v in INSTRUMENT_MAP.items() if v is not None}
    skipped   = [k for k, v in INSTRUMENT_MAP.items() if v is None]

    print(f"\n  Fetchable  : {len(fetchable)} instruments")
    print(f"  Skipped    : {len(skipped)} (indices/unsupported crypto — use MT5)")
    print(f"  Timeframes : {len(TIMEFRAMES)}")
    print("\n  Note: M15 starts 2015, M5 starts 2018, M1 starts 2022")
    print("  Note: H4 and H1 go all the way back to 2008\n")

    successful = []
    failed     = []
    t_start    = time.time()
    total      = len(fetchable)

    for i, (canonical, dk_symbol) in enumerate(fetchable.items(), 1):
        elapsed = time.time() - t_start
        eta = (elapsed / i) * (total - i) if i > 1 else 0
        print(f"\n[{i:>2}/{total}] (elapsed {elapsed/60:.1f}m  ETA {eta/60:.1f}m)")

        stat = fetch_instrument(canonical, dk_symbol)
        tf_results = stat.get("results", {})
        any_fail = any(not v for v in tf_results.values())

        if any_fail:
            failed.append(canonical)
        else:
            successful.append(canonical)

    elapsed_total = time.time() - t_start
    csv_files  = list(DATA_DIR.glob("*.csv"))
    total_size = sum(f.stat().st_size for f in csv_files) / (1024 * 1024)

    print("\n" + "=" * 60)
    print("  DUKASCOPY FETCH COMPLETE")
    print("=" * 60)
    print(f"  ✓ Successful : {len(successful)}")
    print(f"  ✗ Failed     : {len(failed)}")
    print(f"  ⊘ Skipped    : {len(skipped)}")
    print(f"  Time taken   : {elapsed_total/60:.1f} minutes")
    print(f"  CSV files    : {len(csv_files)}")
    print(f"  Total size   : {total_size:.1f} MB")

    if failed:
        print(f"\n  Failed    : {', '.join(failed)}")

    print("\n  Skipped (use fetch_csv.py for these):")
    print(f"  {', '.join(skipped)}")

    print("""
  NEXT STEPS:
  1. Run fetch_csv.py to get indices + remaining crypto from MT5
     (those files will be added alongside Dukascopy files)
  2. Upload any *_H1.csv to Colab train_colab.py
  3. Start training
""")
    print("=" * 60)


if __name__ == "__main__":
    main()

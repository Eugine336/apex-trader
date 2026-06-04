"""
APEX TRADER — fetch_csv.py
===========================
Fetches historical OHLCV data from MT5 for all 49 instruments
across all 5 timeframes and saves to data/.

Broker-aware: reads config/brokers/<broker_slug>.json to resolve
symbol name differences (e.g. US100 → USTEC, GER40 → DE40).
CSV files are always saved under the canonical name (e.g. US100_H1.csv)
so the rest of APEX can find them regardless of broker.

Timeframes fetched:
  H4  — 10,000 bars (~7 years)
  H1  — 50,000 bars (~5.7 years)
  M15 — 50,000 bars (~520 days)
  M5  — 50,000 bars (~174 days)
  M1  — 50,000 bars (~35 days)

Run from project root:
  python fetch_csv.py
"""

import json
import time
import pandas as pd
from pathlib import Path
from datetime import datetime

try:
    import MetaTrader5 as mt5
except ImportError:
    raise SystemExit("MetaTrader5 not installed. Run: pip install MetaTrader5")


# ── Config ────────────────────────────────────────────────────────────────────

DATA_DIR    = Path("data")
BROKER_DIR  = Path("config/brokers")
DATA_DIR.mkdir(exist_ok=True)

TIMEFRAMES = {
    "H4":  (mt5.TIMEFRAME_H4,  10_000),   # ~7 years
    "H1":  (mt5.TIMEFRAME_H1,  50_000),   # ~5.7 years
    "M15": (mt5.TIMEFRAME_M15, 50_000),   # ~520 days
    "M5":  (mt5.TIMEFRAME_M5,  50_000),   # ~174 days
    "M1":  (mt5.TIMEFRAME_M1,  50_000),   # ~35 days
}

MT5_INSTRUMENTS = [
    # Forex majors (7)
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "AUDUSD", "NZDUSD", "USDCAD",
    # Forex crosses (21)
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY",
    "CADJPY", "CHFJPY", "EURCHF", "EURAUD", "EURCAD",
    "EURNZD", "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD",
    "AUDCAD", "AUDCHF", "AUDNZD", "NZDCAD", "NZDCHF", "CADCHF",
    # Commodities (4)
    "XAUUSD", "XAGUSD", "XBRUSD", "XTIUSD",
    # Indices (9 — ESP35 excluded)
    "US100", "US30", "US500", "GER40", "UK100",
    "JP225", "AUS200", "FRA40", "HK50",
    # Crypto (8)
    "BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD",
    "BNBUSD", "SOLUSD", "ADAUSD", "DOTUSD",
]

DERIV_ONLY = [
    "V10_1S", "V25_1S", "V50_1S", "V75_1S", "V100_1S",
    "BOOM500", "BOOM1000", "CRASH500", "CRASH1000", "STPIDX",
    "RNGBULL", "RNGBEAR", "JD10", "JD25", "JD50",
]


# ── Broker symbol map ─────────────────────────────────────────────────────────

def load_symbol_map() -> dict[str, str]:
    """
    Reads the active broker config and returns a dict of
    canonical_name -> broker_name.

    Falls back to identity map if no config found.
    """
    if not mt5.initialize():
        return {}

    # Detect broker slug from MT5
    info = mt5.terminal_info()
    broker_name = ""
    if info:
        broker_name = getattr(info, "company", "") or ""

    # Find matching broker config file
    slug = _broker_slug(broker_name)
    config_path = BROKER_DIR / f"{slug}.json"

    if not config_path.exists():
        # Try any .json in the broker dir
        jsons = list(BROKER_DIR.glob("*.json"))
        if jsons:
            config_path = jsons[0]
            print(f"  [symbol map] No exact broker match — using {config_path.name}")
        else:
            print(f"  [symbol map] No broker config found — using canonical names")
            return {}

    with open(config_path) as f:
        cfg = json.load(f)

    overrides = cfg.get("overrides", {})
    # Filter out _note_ keys
    symbol_map = {
        k: v for k, v in overrides.items()
        if not k.startswith("_") and isinstance(v, str)
    }

    print(f"  [symbol map] Loaded {len(symbol_map)} mappings from {config_path.name}")
    return symbol_map


def _broker_slug(broker_name: str) -> str:
    return broker_name.lower().replace(" ", "_").replace(".", "_").replace("-", "_")


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch(canonical: str, broker_symbol: str, tf_name: str,
          tf_const: int, n_bars: int) -> bool:
    """
    Fetch from MT5 using broker_symbol.
    Save CSV under canonical name so APEX always finds it.
    """
    out = DATA_DIR / f"{canonical}_{tf_name}.csv"

    rates = mt5.copy_rates_from_pos(broker_symbol, tf_const, 0, n_bars)

    if rates is None or len(rates) == 0:
        err = mt5.last_error()
        print(f"    ✗ {tf_name}: no data ({err})")
        return False

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)

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

    broker_note = f" (broker: {broker_symbol})" if broker_symbol != canonical else ""
    print(f"    ✓ {tf_name}: {len(df):,} bars → {out}{broker_note}")
    return True


def fetch_symbol(canonical: str, broker_symbol: str) -> dict:
    mt5.symbol_select(broker_symbol, True)
    time.sleep(0.05)

    info = mt5.symbol_info(broker_symbol)
    if info is None:
        print(f"  ✗ {canonical} ({broker_symbol}) — not found on broker, skipping")
        return {"symbol": canonical, "skipped": True}

    if info.trade_mode == 0:
        print(f"  ⚠ {canonical} ({broker_symbol}) — disabled on broker, fetching anyway")

    mapped_note = f" → {broker_symbol}" if broker_symbol != canonical else ""
    print(f"  {canonical}{mapped_note}")

    results = {}
    for tf_name, (tf_const, n_bars) in TIMEFRAMES.items():
        ok = fetch(canonical, broker_symbol, tf_name, tf_const, n_bars)
        results[tf_name] = ok
        time.sleep(0.02)

    return {"symbol": canonical, "skipped": False, "results": results}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  APEX TRADER — Historical Data Fetch")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print(f"\n  Instruments : {len(MT5_INSTRUMENTS)}")
    print(f"  Timeframes  : {len(TIMEFRAMES)}")
    print(f"  Target files: {len(MT5_INSTRUMENTS) * len(TIMEFRAMES)}")
    print(f"  Est. size   : ~1.2-1.5 GB")
    print(f"  Est. time   : 15-20 minutes\n")

    if not mt5.initialize():
        raise SystemExit(f"MT5 init failed: {mt5.last_error()}")

    account = mt5.account_info()
    if account:
        print(f"  Connected : {account.login} | {account.server} | ${account.balance:.2f}")

    # Load broker symbol map
    symbol_map = load_symbol_map()
    print()

    total      = len(MT5_INSTRUMENTS)
    skipped    = []
    failed     = []
    successful = []
    t_start    = time.time()

    for i, canonical in enumerate(MT5_INSTRUMENTS, 1):
        elapsed = time.time() - t_start
        eta = (elapsed / i) * (total - i) if i > 1 else 0
        print(f"[{i:>2}/{total}] (elapsed {elapsed/60:.1f}m  ETA {eta/60:.1f}m)")

        # Resolve broker name — fall back to canonical if not mapped
        broker_symbol = symbol_map.get(canonical, canonical)

        stat = fetch_symbol(canonical, broker_symbol)

        if stat.get("skipped"):
            skipped.append(canonical)
        else:
            tf_results = stat.get("results", {})
            any_fail = any(not v for v in tf_results.values())
            if any_fail:
                failed.append(canonical)
            else:
                successful.append(canonical)

        time.sleep(0.1)

    mt5.shutdown()

    elapsed_total = time.time() - t_start
    csv_files  = list(DATA_DIR.glob("*.csv"))
    total_size = sum(f.stat().st_size for f in csv_files) / (1024 * 1024)

    print("\n" + "=" * 60)
    print("  FETCH COMPLETE")
    print("=" * 60)
    print(f"  ✓ Successful : {len(successful)}")
    print(f"  ✗ Failed     : {len(failed)}")
    print(f"  ⊘ Skipped    : {len(skipped)}")
    print(f"  Time taken   : {elapsed_total/60:.1f} minutes")
    print(f"  CSV files    : {len(csv_files)}")
    print(f"  Total size   : {total_size:.1f} MB")

    if failed:
        print(f"\n  Failed    : {', '.join(failed)}")
    if skipped:
        print(f"  Skipped   : {', '.join(skipped)}")

    print(f"\n  Deriv synthetics (fetch separately via Deriv API):")
    print(f"  {', '.join(DERIV_ONLY)}")
    print("=" * 60)
    print("\n  Data ready. Upload any *_H1.csv to Colab to start training.")


if __name__ == "__main__":
    main()

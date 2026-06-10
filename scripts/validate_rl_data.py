#!/usr/bin/env python3
"""
APEX RL — Data Quality Validation
====================================
Validates multi-TF OHLCV CSVs for RL training readiness.

Checks time continuity, OHLC sanity, volume, cross-TF alignment,
and minimum bar counts. Returns exit code 0 (all OK) or 1 (issues found).

Pure stdlib + pandas. No torch dependency.

Usage::

    python scripts/validate_rl_data.py
    python scripts/validate_rl_data.py --data-dir data --min-bars 2000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

REQUIRED_TFS = ["H4", "H1", "M15", "M5"]
ALL_TFS = ["M1", "M5", "M15", "H1", "H4"]

TF_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
}

FOREX_SYMBOLS = {
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY",
    "EURCHF", "EURAUD", "EURCAD", "EURNZD", "GBPAUD", "GBPCAD", "GBPCHF",
    "GBPNZD", "AUDCAD", "AUDCHF", "AUDNZD", "NZDCAD", "NZDCHF", "CADCHF",
}


def _parse_filename(name: str) -> tuple[str, str] | None:
    stem = name.removesuffix(".csv")
    for tf in ALL_TFS:
        suffix = f"_{tf}"
        if stem.endswith(suffix):
            return stem[: -len(suffix)], tf
    return None


def _check_ohlc_sanity(df: pd.DataFrame) -> list[str]:
    issues = []
    bad_hl = (df["high"] < df["low"]).sum()
    if bad_hl > 0:
        issues.append(f"{bad_hl} bars where high < low")

    bad_ho = (df["high"] < df[["open", "close"]].max(axis=1)).sum()
    if bad_ho > 0:
        issues.append(f"{bad_ho} bars where high < max(open, close)")

    bad_lo = (df["low"] > df[["open", "close"]].min(axis=1)).sum()
    if bad_lo > 0:
        issues.append(f"{bad_lo} bars where low > min(open, close)")

    return issues


def _check_time_gaps(df: pd.DataFrame, tf: str, symbol: str) -> list[str]:
    issues = []
    expected_seconds = TF_SECONDS.get(tf, 300)
    max_gap = expected_seconds * 2

    times = pd.to_datetime(df["time"])
    diffs = times.diff().dt.total_seconds().dropna()

    is_forex = symbol.upper() in FOREX_SYMBOLS

    if is_forex:
        weekday_mask = times.iloc[1:].dt.dayofweek < 5
        weekday_diffs = diffs[weekday_mask.values]
        large_gaps = (weekday_diffs > max_gap).sum()
    else:
        large_gaps = (diffs > max_gap).sum()

    if large_gaps > 0:
        issues.append(f"{large_gaps} gaps > {max_gap}s ({tf})")

    return issues


def _check_volume(df: pd.DataFrame) -> list[str]:
    issues = []
    if "volume" in df.columns:
        zero_vol = (df["volume"] == 0).sum()
        if zero_vol > 0:
            issues.append(f"{zero_vol} zero-volume bars")
    return issues


def _check_weekend_bars(df: pd.DataFrame, symbol: str) -> list[str]:
    issues = []
    if symbol.upper() not in FOREX_SYMBOLS:
        return issues
    times = pd.to_datetime(df["time"])
    weekend_mask = times.dt.dayofweek >= 5
    weekend_count = weekend_mask.sum()
    if weekend_count > 0:
        issues.append(f"{weekend_count} weekend bars (forex)")
    return issues


def _check_cross_tf_alignment(data: dict[str, pd.DataFrame], symbol: str) -> list[str]:
    issues = []
    if "M5" not in data or "H1" not in data:
        return issues

    m5 = data["M5"]
    h1 = data["H1"]

    m5_times = pd.to_datetime(m5["time"])
    h1_times = pd.to_datetime(h1["time"])

    m5_start = m5_times.min()
    m5_end = m5_times.max()
    h1_start = h1_times.min()
    h1_end = h1_times.max()

    if h1_start > m5_start + pd.Timedelta(days=1):
        issues.append(f"H1 starts {(h1_start - m5_start).days}d after M5")
    if h1_end < m5_end - pd.Timedelta(days=1):
        issues.append(f"H1 ends {(m5_end - h1_end).days}d before M5")

    return issues


def validate(data_dir: str, min_bars: int) -> int:
    data_path = Path(data_dir)
    if not data_path.is_dir():
        print(f"ERROR: {data_dir} is not a directory", file=sys.stderr)
        return 1

    coverage: dict[str, dict[str, pd.DataFrame]] = {}

    for entry in sorted(data_path.iterdir()):
        if not entry.name.endswith(".csv"):
            continue
        parsed = _parse_filename(entry.name)
        if parsed is None:
            continue
        symbol, tf = parsed
        try:
            df = pd.read_csv(str(entry), parse_dates=["time"])
            df.columns = df.columns.str.lower()
        except Exception as e:
            print(f"  ERROR reading {entry.name}: {e}")
            continue
        if symbol not in coverage:
            coverage[symbol] = {}
        coverage[symbol][tf] = df

    if not coverage:
        print("No instrument CSVs found.", file=sys.stderr)
        return 1

    total_issues = 0
    usable = []

    for sym in sorted(coverage):
        data = coverage[sym]
        sym_issues: list[str] = []

        missing = [tf for tf in REQUIRED_TFS if tf not in data]
        if missing:
            sym_issues.append(f"MISSING {','.join(missing)}")

        for tf, df in data.items():
            if len(df) < min_bars and tf in REQUIRED_TFS:
                sym_issues.append(f"{tf}: {len(df)} bars < {min_bars}")
            sym_issues.extend(_check_ohlc_sanity(df))
            sym_issues.extend(_check_time_gaps(df, tf, sym))
            sym_issues.extend(_check_volume(df))
            sym_issues.extend(_check_weekend_bars(df, sym))

        sym_issues.extend(_check_cross_tf_alignment(data, sym))

        status = "OK" if not sym_issues else "ISSUES"
        bar_counts = {tf: len(df) for tf, df in sorted(data.items())}
        print(f"\n{sym:<12} bars={bar_counts}  [{status}]")
        for issue in sym_issues:
            print(f"  ⚠ {issue}")
            total_issues += 1

        if not sym_issues and not missing:
            usable.append(sym)

    print(f"\n{'='*60}")
    print(f"SUMMARY: {len(coverage)} instruments, {len(usable)} usable, {total_issues} issues")
    if usable:
        print(f"  Usable: {', '.join(usable)}")

    return 0 if total_issues == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="APEX RL data quality validation")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--min-bars", type=int, default=2000)
    args = parser.parse_args()
    sys.exit(validate(args.data_dir, args.min_bars))


if __name__ == "__main__":
    main()

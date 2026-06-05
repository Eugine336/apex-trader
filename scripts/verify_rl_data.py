#!/usr/bin/env python3
"""
APEX RL — Phase 0 Data Census
===============================
Scans ``data/`` for OHLCV CSVs, builds an instrument × timeframe
coverage matrix, and flags instruments missing any of H4/H1/M15/M5
or below a ``--min-bars`` threshold.

Pure stdlib — no pandas / numpy required.

Usage::

    python scripts/verify_rl_data.py
    python scripts/verify_rl_data.py --min-bars 2000
    python scripts/verify_rl_data.py --data-dir /path/to/data
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

REQUIRED_TFS = ["H4", "H1", "M15", "M5"]
ALL_TFS = ["M1", "M5", "M15", "H1", "H4"]


def _count_rows(path: str) -> int:
    count = 0
    with open(path, newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for _ in reader:
            count += 1
    return count


def _parse_filename(name: str) -> tuple[str, str] | None:
    stem = name.removesuffix(".csv")
    for tf in ALL_TFS:
        suffix = f"_{tf}"
        if stem.endswith(suffix):
            symbol = stem[: -len(suffix)]
            return symbol, tf
    return None


def run(data_dir: str, min_bars: int) -> int:
    data_path = Path(data_dir)
    if not data_path.is_dir():
        print(f"ERROR: {data_dir} is not a directory", file=sys.stderr)
        return 1

    coverage: dict[str, dict[str, int]] = defaultdict(dict)

    for entry in sorted(os.listdir(data_path)):
        if not entry.endswith(".csv"):
            continue
        parsed = _parse_filename(entry)
        if parsed is None:
            continue
        symbol, tf = parsed
        full = str(data_path / entry)
        count = _count_rows(full)
        coverage[symbol][tf] = count

    if not coverage:
        print("No instrument CSVs found.", file=sys.stderr)
        return 1

    usable: list[str] = []
    excluded: list[tuple[str, str]] = []

    print(f"\n{'Symbol':<12}", end="")
    for tf in ALL_TFS:
        print(f"{tf:>8}", end="")
    print(f"  {'Status':>10}")
    print("-" * 68)

    for sym in sorted(coverage):
        row = coverage[sym]
        print(f"{sym:<12}", end="")
        for tf in ALL_TFS:
            bars = row.get(tf, 0)
            print(f"{bars:>8}", end="")

        missing = [tf for tf in REQUIRED_TFS if tf not in row]
        below = [tf for tf in REQUIRED_TFS if row.get(tf, 0) < min_bars]

        if missing:
            reason = f"MISSING {','.join(missing)}"
            excluded.append((sym, reason))
            print(f"  {'EXCLUDE':>10}  ({reason})")
        elif below:
            reason = f"<{min_bars} bars on {','.join(below)}"
            excluded.append((sym, reason))
            print(f"  {'EXCLUDE':>10}  ({reason})")
        else:
            usable.append(sym)
            print(f"  {'OK':>10}")

    print("-" * 68)
    print("\nSUMMARY")
    print(f"  Found:    {len(coverage)} instruments")
    print(f"  Usable:   {len(usable)} (all required TFs with >= {min_bars} bars)")
    print(f"  Excluded: {len(excluded)}")

    if excluded:
        print("\n  Excluded instruments:")
        for sym, reason in excluded:
            print(f"    {sym:<12} {reason}")

    print(
        "\n  NOTE: Synthetics (V75, BOOM, CRASH, STPIDX) have no CSVs in data/ "
        "and run scanner-only — they cannot be RL-trained.\n"
    )

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="APEX RL data census")
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Path to the data directory (default: data)",
    )
    parser.add_argument(
        "--min-bars",
        type=int,
        default=1500,
        help="Minimum bars per required TF (default: 1500)",
    )
    args = parser.parse_args()
    sys.exit(run(args.data_dir, args.min_bars))


if __name__ == "__main__":
    main()

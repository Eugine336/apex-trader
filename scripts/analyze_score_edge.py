"""
APEX TRADER — Score-Edge Attribution

Measures whether the live entry *score* has predictive edge: does a higher
score actually produce higher realized expectancy?

Reads the trade journal (``data/trade_journal.db`` by default), buckets
trades by score, computes Spearman / Pearson correlations, and prints a
verdict.

Usage:
    python scripts/analyze_score_edge.py
    python scripts/analyze_score_edge.py --db path/to/trade_journal.db
    python scripts/analyze_score_edge.py --min-samples 50

DATA PREREQUISITE: the verdict is only meaningful against a populated
trade_journal.db from live or paper trading.  An empty or sparse journal
yields INSUFFICIENT_DATA by design — this is not an error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.trade_journal import TradeJournal  # noqa: E402
from adaptive.trade_analyzer import TradeAnalyzer  # noqa: E402


def _print_table(
    label: str,
    profiles: dict,
) -> None:
    """Print a score-bucket performance table."""
    if not profiles:
        print(f"\n  {label}: no qualifying trades in any bucket.")
        return
    print(f"\n  {label}:")
    header = f"    {'Bucket':<22} {'N':>5}  {'WinRate':>8}  {'Expectancy':>11}  {'PF':>8}  {'Sharpe':>7}"
    print(header)
    print("    " + "-" * (len(header) - 4))
    for bucket, prof in profiles.items():
        pf_str = f"{prof.profit_factor:.2f}" if prof.profit_factor != float("inf") else "inf"
        print(
            f"    {bucket:<22} {prof.total_trades:>5}  "
            f"{prof.win_rate:>7.1%}  {prof.expectancy:>+11.4f}  "
            f"{pf_str:>8}  {prof.sharpe_ratio:>7.2f}"
        )


async def run(db_path: Path, min_samples: int) -> None:
    journal = TradeJournal(db_path=str(db_path))
    trades = await journal.get_all_trades_as_dicts()

    print(f"Database : {db_path}")
    print(f"Total trades in journal: {len(trades)}")

    analyzer = TradeAnalyzer()

    # --- bucket profiles ---
    buckets = analyzer.analyze_by_score(trades)
    _print_table("Quantile buckets (deciles)", buckets["quantile"])
    _print_table("Fixed score bands", buckets["fixed"])

    # --- edge verdict ---
    edge = analyzer.score_edge(trades, min_samples=min_samples)

    print("\n  Correlation analysis:")
    print(f"    Qualifying trades : {edge['sample_count']}")
    print(f"    Spearman (rank)   : {edge['spearman']:+.4f}")
    print(f"    Pearson (raw)     : {edge['pearson']:+.4f}")

    if edge["band_expectancies"]:
        print("\n    Per-band expectancy:")
        for band, exp in edge["band_expectancies"].items():
            print(f"      {band:<10} → {exp:+.4f}")
        print(f"    Monotonicity inversions: {edge['monotonicity_inversions']}")

    verdict = edge["verdict"]
    print(f"\n  ══════════════════════════════════════")
    print(f"  VERDICT: {verdict}")
    print(f"  ══════════════════════════════════════")

    if verdict == "INSUFFICIENT_DATA":
        print(
            "\n  INSUFFICIENT DATA — accumulate more journaled trades"
            "\n  (e.g. run in paper mode) before drawing a conclusion."
        )
    elif verdict == "POSITIVE_EDGE":
        print(
            "\n  Higher scores correlate with better realized P&L."
            "\n  The adaptive scoring signal appears to carry predictive edge."
        )
    elif verdict == "INVERTED_EDGE":
        print(
            "\n  Higher scores correlate with WORSE realized P&L."
            "\n  The scoring model may be counter-productive — investigate."
        )
    else:
        print(
            "\n  No statistically clear relationship between score and P&L."
            "\n  The adaptive signal is not demonstrably helpful or harmful."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure whether the live entry score has predictive edge.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/trade_journal.db"),
        help="Path to trade_journal.db (default: data/trade_journal.db)",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=30,
        help="Minimum qualifying trades before producing a verdict (default: 30)",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"Database not found: {args.db}")
        print(
            "INSUFFICIENT DATA — accumulate more journaled trades"
            " (e.g. run in paper mode) before drawing a conclusion."
        )
        sys.exit(0)

    asyncio.run(run(args.db, min_samples=args.min_samples))


if __name__ == "__main__":
    main()

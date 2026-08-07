"""
APEX TRADER — Legacy Journal Cleanup

Identifies and flags (or deletes) legacy trade-journal rows that were written
before the P&L accuracy fix.  Legacy rows are untrustworthy because they store
pips in the ``pnl`` column while the dashboard reads the value as dollars, and
they record ``entry = 0 / exit = 0`` because the fill price was never captured.

Detection criteria (all must be true):
  * outcome IN ('WIN', 'LOSS', 'BREAKEVEN')  — i.e. a *closed* trade
  * entry = 0 AND exit = 0

Default mode is **dry-run** (no writes).  Pass ``--apply`` to flag rows with
``outcome = 'LEGACY'`` (non-destructive).  Pass ``--apply --delete`` to remove
them entirely.

The dashboard state helpers, performance mixin, and the TradeJournal query
methods all exclude ``outcome = 'LEGACY'`` so flagged rows are hidden from
every aggregation (win/loss counts, equity curve, total P&L, ML feed).

Usage:
    python scripts/clean_legacy_journal.py              # dry-run
    python scripts/clean_legacy_journal.py --apply      # flag as LEGACY
    python scripts/clean_legacy_journal.py --apply --delete  # delete rows
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path("data/trade_journal.db")

CLOSED_OUTCOMES = ("WIN", "LOSS", "BREAKEVEN")

DETECT_SQL = """
    SELECT id, pair, direction, entry, exit, pnl, outcome, timestamp
    FROM trades
    WHERE outcome IN ({placeholders})
      AND entry = 0
      AND exit  = 0
""".format(placeholders=", ".join("?" for _ in CLOSED_OUTCOMES))

SUMMARY_SQL = """
    SELECT pair, outcome, COUNT(*) AS cnt
    FROM trades
    WHERE outcome IN ({placeholders})
      AND entry = 0
      AND exit  = 0
    GROUP BY pair, outcome
    ORDER BY cnt DESC
""".format(placeholders=", ".join("?" for _ in CLOSED_OUTCOMES))


def _backup(db_path: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.parent / f"{db_path.stem}.backup_{ts}{db_path.suffix}"
    shutil.copy2(db_path, backup)
    return backup


def _detect(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(DETECT_SQL, CLOSED_OUTCOMES).fetchall()


def _print_summary(conn: sqlite3.Connection) -> int:
    rows = conn.execute(SUMMARY_SQL, CLOSED_OUTCOMES).fetchall()
    total = 0
    if rows:
        print(f"\n{'Symbol':<14} {'Outcome':<12} {'Count':>6}")
        print("-" * 34)
        for r in rows:
            print(f"{r[0]:<14} {r[1]:<12} {r[2]:>6}")
            total += r[2]
        print("-" * 34)
    print(f"{'TOTAL':<27} {total:>6}")
    return total


def _print_samples(rows: list[sqlite3.Row], limit: int = 5) -> None:
    if not rows:
        return
    print(f"\nSample rows (showing {min(limit, len(rows))} of {len(rows)}):")
    print(f"  {'id':>5}  {'pair':<12} {'dir':<6} {'entry':>10} {'exit':>10} {'pnl':>10} {'outcome':<10} {'timestamp'}")
    print("  " + "-" * 90)
    for r in rows[:limit]:
        print(
            f"  {r['id']:>5}  {r['pair']:<12} {r['direction']:<6} "
            f"{r['entry']:>10.5f} {r['exit']:>10.5f} {r['pnl']:>10.2f} "
            f"{r['outcome']:<10} {r['timestamp']}"
        )


def run(db_path: Path, *, apply: bool = False, delete: bool = False) -> dict:
    """Execute the cleanup. Returns a summary dict for programmatic use."""
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return {"matched": 0, "action": "none", "backup": None}

    conn = sqlite3.connect(str(db_path))
    try:
        matched = _print_summary(conn)
        legacy_rows = _detect(conn)
        _print_samples(legacy_rows)

        if matched == 0:
            print("\nNo legacy rows found — nothing to do.")
            return {"matched": 0, "action": "none", "backup": None}

        if not apply:
            print(
                f"\n[DRY-RUN] {matched} rows would be affected. "
                "Re-run with --apply to flag or --apply --delete to remove."
            )
            return {"matched": matched, "action": "dry_run", "backup": None}

        backup = _backup(db_path)
        print(f"\nBackup created: {backup}")

        ids = [r["id"] for r in legacy_rows]
        placeholders = ", ".join("?" for _ in ids)

        if delete:
            conn.execute(f"DELETE FROM trades WHERE id IN ({placeholders})", ids)
            action = "deleted"
        else:
            conn.execute(
                f"UPDATE trades SET outcome = 'LEGACY' WHERE id IN ({placeholders})",
                ids,
            )
            action = "flagged"

        conn.commit()
        print(f"[APPLIED] {matched} rows {action}.")

        remaining = _detect(conn)
        assert len(remaining) == 0, "Idempotency check failed — rows remain"

        return {"matched": matched, "action": action, "backup": str(backup)}

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean legacy trade-journal rows (pips-as-dollars, entry/exit=0).")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to trade_journal.db (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually mutate the database (default is dry-run).",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete legacy rows instead of flagging them as LEGACY.",
    )
    args = parser.parse_args()

    if args.delete and not args.apply:
        print("ERROR: --delete requires --apply", file=sys.stderr)
        sys.exit(1)

    run(args.db, apply=args.apply, delete=args.delete)


if __name__ == "__main__":
    main()

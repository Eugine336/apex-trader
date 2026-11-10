"""Seed observational reasoning-strength diagnostics from market outcomes.

This loads reported reasoning-strength/outcome pairs from a hindcast replay,
demo/shadow results, or exported live closes into
``<data_dir>/brain_calibration.json`` for governance and diagnostic reporting.
The legacy input column/key is named ``confidence``, but these values are
uncalibrated reasoning metadata, not win probabilities. The resulting metrics
are observational only: they do not qualify opportunities, change expected
return, authorize decisions, or scale position size.

Input: JSON Lines (one object per line) or CSV with columns ``confidence`` and
``won`` (true/false/1/0). The legacy field name is retained for input
compatibility. Usage::

    python scripts/seed_calibration.py outcomes.jsonl --source hindcast
    python scripts/seed_calibration.py outcomes.csv --source demo --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y", "win", "won"}


def read_pairs(path: Path) -> list[tuple[float, bool]]:
    pairs: list[tuple[float, bool]] = []
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pairs.append((float(row["confidence"]), _truthy(row["won"])))
    else:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                pairs.append((float(obj["confidence"]), _truthy(obj["won"])))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("input", type=Path)
    parser.add_argument("--source", default="hindcast", help="label for the outcome source")
    parser.add_argument("--dry-run", action="store_true", help="report only; do not write")
    args = parser.parse_args(argv)

    from cognition.influence import CalibrationTracker
    from runtime_paths import data_dir

    pairs = read_pairs(args.input)
    path = data_dir() / "brain_calibration.json"
    tracker = CalibrationTracker(path=None if args.dry_run else path)
    accepted = tracker.observe_many(pairs, source=args.source)
    status = tracker.get_status()
    print(f"accepted {accepted}/{len(pairs)} pairs from '{args.source}'"
          f"{' (dry run)' if args.dry_run else f' -> {path}'}")
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

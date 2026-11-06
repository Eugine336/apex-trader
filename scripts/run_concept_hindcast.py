"""Replay all native concepts and the Brain over attested historical OHLC.

This command is offline. It does not connect to a broker, create quote evidence,
or submit orders. Calibration persistence is opt-in via --seed-calibration.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest", type=Path, help="SHA-256 checked historical OHLC manifest")
    parser.add_argument("--base-timeframe", default="M5")
    parser.add_argument("--horizon-seconds", type=int, required=True,
                        help="explicit outcome horizon; no implicit trading horizon is assumed")
    parser.add_argument("--decision-stride", type=int, default=1,
                        help="evaluate every Nth completed base-timeframe bar")
    parser.add_argument("--output", type=Path, default=None,
                        help="optional JSON report path (stdout when omitted)")
    parser.add_argument("--seed-calibration", action="store_true",
                        help="persist resolved Brain confidence/outcome pairs")
    parser.add_argument("--calibration-path", type=Path, default=None,
                        help="optional CalibrationTracker path; defaults to runtime data directory")
    args = parser.parse_args(argv)

    from cognition.hindcast import (
        load_hindcast_dataset,
        run_hindcast,
        seed_calibration_tracker,
    )
    from cognition.influence import CalibrationTracker

    dataset = load_hindcast_dataset(args.manifest)
    report = run_hindcast(
        dataset,
        max_horizon_seconds=args.horizon_seconds,
        base_timeframe=args.base_timeframe,
        decision_stride=args.decision_stride,
    )
    payload = report.to_dict()

    if args.seed_calibration:
        if not report.brain_calibration_pairs:
            parser.error("no resolved Brain candidate outcomes; calibration was not changed")
        if args.calibration_path is None:
            from runtime_paths import data_dir
            calibration_path = data_dir() / "brain_calibration.json"
        else:
            calibration_path = args.calibration_path
        tracker = CalibrationTracker(path=calibration_path)
        accepted = seed_calibration_tracker(tracker, report)
        payload["calibration_seed"] = {
            "accepted": accepted,
            "path": str(calibration_path),
            "status": tracker.get_status(),
        }

    rendered = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)
    if args.output is None:
        print(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"hindcast report written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

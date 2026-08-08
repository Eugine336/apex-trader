"""Command-line interface for the backtest harness.

Examples
--------
Run on a CSV::

    python -m backtest.cli --data data/EURUSD_1h.csv --pair EURUSD --timeframe H1

Run on synthetic data::

    python -m backtest.cli --synthetic trending --candles 5000 --pair EURUSD

Config overrides + exports::

    python -m backtest.cli --data data/EURUSD_1h.csv \
        --config-override '{"spread_pips": 1.5}' \
        --output results/run_001.json --trade-log results/trades_001.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backtest.data import HistoricalDataLoader
from backtest.results import BacktestReporter
from backtest.runner import BacktestRunner
from backtest.synthetic_data import GENERATORS, generate


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backtest", description="APEX Trader backtest harness",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--data", help="Path to a CSV or JSON OHLCV file")
    src.add_argument(
        "--synthetic", choices=sorted(GENERATORS),
        help="Generate a synthetic scenario instead of loading a file",
    )
    p.add_argument("--pair", default="EURUSD", help="Instrument symbol")
    p.add_argument("--timeframe", default="M5", help="Base timeframe (M1/M5/M15/H1/H4/D1)")
    p.add_argument("--candles", type=int, default=2000, help="Synthetic bar count")
    p.add_argument("--seed", type=int, default=42, help="RNG seed (reproducible runs)")
    p.add_argument("--balance", type=float, default=10_000.0, help="Starting balance")
    p.add_argument("--spread", type=float, default=1.0, help="Spread in pips")
    p.add_argument("--slippage", type=float, default=0.5, help="Slippage in pips")
    p.add_argument("--no-regime", action="store_true", help="Disable the L7 regime detector")
    p.add_argument("--no-risk", action="store_true", help="Disable the L8 risk gate")
    p.add_argument(
        "--config-override", default="",
        help="JSON object of runner overrides (e.g. '{\"spread_pips\": 1.5}')",
    )
    p.add_argument("--output", help="Write full results JSON to this path")
    p.add_argument("--trade-log", help="Write the trade log CSV to this path")
    p.add_argument(
        "--progress-interval", type=int, default=500,
        help="Print progress every N bars (0 = silent)",
    )
    return p


def _load_candles(args) -> list:
    if args.synthetic:
        return generate(
            args.synthetic, candles=args.candles, pair=args.pair,
            timeframe=args.timeframe, seed=args.seed,
        )
    loader = HistoricalDataLoader()
    return loader.load(args.data, pair=args.pair)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        candles = _load_candles(args)
    except Exception as exc:  # noqa: BLE001
        print(f"error: failed to load data: {exc}", file=sys.stderr)
        return 2
    if not candles:
        print("error: no candles to replay", file=sys.stderr)
        return 2

    overrides = {}
    if args.config_override:
        try:
            overrides = json.loads(args.config_override)
        except json.JSONDecodeError as exc:
            print(f"error: --config-override is not valid JSON: {exc}", file=sys.stderr)
            return 2

    runner = BacktestRunner(
        data={args.pair: {args.timeframe: candles}},
        base_timeframe=args.timeframe,
        starting_balance=overrides.get("starting_balance", args.balance),
        spread_pips=overrides.get("spread_pips", args.spread),
        slippage_pips=overrides.get("slippage_pips", args.slippage),
        seed=overrides.get("seed", args.seed),
        progress_interval=args.progress_interval,
        use_regime=not args.no_regime,
        use_risk=not args.no_risk,
    )
    try:
        results = runner.run()
    finally:
        pass

    reporter = BacktestReporter(results)
    print(reporter.console_summary())

    if args.output:
        reporter.to_json(args.output)
        print(f"[backtest] results JSON → {Path(args.output)}")
    if args.trade_log:
        reporter.to_trade_csv(args.trade_log)
        print(f"[backtest] trade log CSV → {Path(args.trade_log)}")

    runner.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

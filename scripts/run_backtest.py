"""
APEX TRADER — Backtest Runner
Usage:
    python scripts/run_backtest.py --pair EURUSD --platform mt5 --bars 10000
    python scripts/run_backtest.py --pair EURUSD --csv M1:data/EURUSD_M1.csv M5:data/EURUSD_M5.csv
    python scripts/run_backtest.py --pair EURUSD --compare-atr-stop --csv M1:data/EURUSD_M1.csv ...
"""
import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from brain.backtest_engine import BacktestEngine, DataLoader
from loguru import logger


def main():
    parser = argparse.ArgumentParser(description="Apex Trader Backtest Runner")
    parser.add_argument("--pair", required=True, help="Symbol e.g. EURUSD")
    parser.add_argument("--platform", default="mt5", choices=["mt5", "deriv"])
    parser.add_argument("--bars", type=int, default=5000)
    parser.add_argument("--balance", type=float, default=10000.0)
    parser.add_argument("--risk", type=float, default=0.01,
                        help="Risk per trade as decimal (default 0.01 = 1%%)")
    parser.add_argument("--slippage", type=float, default=1.0,
                        help="Average slippage in pips (default 1.0)")
    parser.add_argument("--commission", type=float, default=3.5,
                        help="Commission per lot USD round-trip (default 3.5)")
    parser.add_argument("--csv", nargs="*",
                        help="CSV files instead of broker fetch. "
                             "Format: TF:path e.g. M1:data/eu_m1.csv H1:data/eu_h1.csv")
    parser.add_argument("--compare-atr-stop", action="store_true", default=False,
                        help="Run ATR volatility-stop counterfactual alongside structure stop")
    parser.add_argument("--atr-stop-period", type=int, default=14,
                        help="ATR lookback period (default 14)")
    parser.add_argument("--atr-stop-mult", type=float, default=1.5,
                        help="ATR multiplier for stop distance (default 1.5)")
    parser.add_argument("--atr-stop-ratio-min", type=float, default=0.5,
                        help="Minimum ratio of ATR stop to structure stop (default 0.5)")
    parser.add_argument("--atr-stop-ratio-max", type=float, default=2.0,
                        help="Maximum ratio of ATR stop to structure stop (default 2.0)")
    parser.add_argument("--atr-stop-max-risk-mult", type=float, default=4.0,
                        help="Maximum multiple of min_risk_pips for ATR stop (default 4.0)")
    args = parser.parse_args()

    if args.platform == "mt5" and not args.csv:
        import MetaTrader5 as mt5

        if not mt5.initialize():
            logger.error(f"MT5 failed to initialize: {mt5.last_error()}")
            return

        login = int(os.getenv("MT5_LOGIN", "0"))
        password = os.getenv("MT5_PASSWORD", "")
        server = os.getenv("MT5_SERVER", "")

        if login:
            if not mt5.login(login, password=password, server=server):
                logger.error(f"MT5 login failed: {mt5.last_error()}")
                mt5.shutdown()
                return

        account = mt5.account_info()
        account_login = account.login if account else "unknown"
        logger.info(f"MT5 initialized — account {account_login}")

    engine = BacktestEngine(
        starting_balance=args.balance,
        risk_per_trade=args.risk,
        slippage_pips=args.slippage,
        commission_per_lot=args.commission,
    )

    atr_kwargs = dict(
        compare_atr_stop=args.compare_atr_stop,
        atr_stop_period=args.atr_stop_period,
        atr_stop_mult=args.atr_stop_mult,
        atr_stop_ratio_min=args.atr_stop_ratio_min,
        atr_stop_ratio_max=args.atr_stop_ratio_max,
        atr_stop_max_risk_mult=args.atr_stop_max_risk_mult,
    )

    if args.csv:
        loader = DataLoader()
        data = {}
        for entry in args.csv:
            tf, path = entry.split(":", 1)
            data[tf] = loader.load_csv(path)
        result = engine.run(pair=args.pair, data_by_timeframe=data, **atr_kwargs)
    else:
        logger.info(f"Fetching {args.bars} bars for {args.pair} from {args.platform}…")
        data = engine.broker_loader.fetch_all_timeframes(
            symbol=args.pair,
            timeframes=["H4", "H1", "M15", "M5", "M1"],
            platform=args.platform,
            bars=args.bars,
        )
        result = engine.run(pair=args.pair, data_by_timeframe=data, **atr_kwargs)

    logger.info("=" * 50)
    logger.info(f"BACKTEST RESULTS — {args.pair}")
    logger.info("=" * 50)
    logger.info(f"Total trades:           {result.total_trades}")
    logger.info(f"Win rate:               {result.win_rate:.1f}%")
    logger.info(f"Gross profit factor:    {result.gross_profit_factor:.2f}")
    logger.info(f"Net profit factor:      {result.net_profit_factor:.2f}")
    logger.info(f"Sharpe ratio:           {result.sharpe_ratio:.2f}")
    logger.info(f"Max drawdown:           {result.max_drawdown:.1%}")
    logger.info(f"Max consecutive losses: {result.max_consecutive_losses}")
    logger.info(f"Expectancy:             {result.expectancy:.4f}")
    logger.info(f"Avg hold time (bars):   {result.avg_hold_time:.1f}")
    logger.info(f"Total commission:       ${result.total_commission:.2f}")
    logger.info(f"Total slippage cost:    {result.total_slippage_cost:.6f}")
    logger.info(f"Best session:           {result.best_session}")
    logger.info(f"Equity curve points:    {len(result.equity_curve)}")

    if result.atr_comparison is not None:
        cmp = result.atr_comparison
        logger.info("")
        logger.info("=" * 50)
        logger.info("ATR VOLATILITY STOP — COMPARISON")
        logger.info("=" * 50)
        logger.info(f"Trades compared:        {cmp.total_compared}")
        logger.info(f"Trades skipped (no ATR):{cmp.total_skipped}")
        logger.info("")
        logger.info(f"  {'':24s} {'STRUCTURE':>12s} {'ATR':>12s}")
        logger.info(f"  {'─' * 48}")
        logger.info(f"  {'Wins':24s} {cmp.structure_wins:12d} {cmp.atr_wins:12d}")
        logger.info(f"  {'Losses':24s} {cmp.structure_losses:12d} {cmp.atr_losses:12d}")
        logger.info(f"  {'Breakevens':24s} {cmp.structure_breakevens:12d} {cmp.atr_breakevens:12d}")
        logger.info(f"  {'Win rate':24s} {cmp.structure_win_rate:11.1f}% {cmp.atr_win_rate:11.1f}%")
        logger.info(f"  {'Loss rate':24s} {cmp.structure_loss_rate:11.1f}% {cmp.atr_loss_rate:11.1f}%")
        logger.info(f"  {'Mean R':24s} {cmp.structure_mean_r:12.3f} {cmp.atr_mean_r:12.3f}")
        logger.info(f"  {'Expectancy (R)':24s} {cmp.structure_expectancy:12.4f} {cmp.atr_expectancy:12.4f}")
        logger.info("")
        logger.info("  ══════════════════════════════════════════════")
        delta_sign = "+" if cmp.expectancy_delta >= 0 else ""
        logger.info(f"  EXPECTANCY DELTA (ATR − Structure): {delta_sign}{cmp.expectancy_delta:.4f} R")
        if cmp.expectancy_delta > 0:
            logger.info("  ATR stop OUTPERFORMED structure stop")
        elif cmp.expectancy_delta < 0:
            logger.info("  Structure stop OUTPERFORMED ATR stop")
        else:
            logger.info("  No difference between stop methods")
        logger.info("  ══════════════════════════════════════════════")
    elif args.compare_atr_stop:
        logger.info("")
        logger.info("ATR comparison requested but no comparison data produced")
        logger.info("(likely zero eligible trades or ATR unavailable for all setups)")

    out_path = f"data/backtest_{args.pair}_{args.platform}.csv"
    Path("data").mkdir(exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bar", "equity"])
        for i, eq in enumerate(result.equity_curve):
            w.writerow([i, round(eq, 2)])
    logger.info(f"Equity curve saved to: {out_path}")


if __name__ == "__main__":
    main()

"""
APEX TRADER — Backtest Runner
Usage:
    python scripts/run_backtest.py --pair EURUSD --platform mt5 --bars 10000
    python scripts/run_backtest.py --pair EURUSD --csv M1:data/EURUSD_M1.csv M5:data/EURUSD_M5.csv
"""
import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from brain.backtest_engine import BacktestEngine, BrokerDataLoader, DataLoader
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

    if args.csv:
        loader = DataLoader()
        data = {}
        for entry in args.csv:
            tf, path = entry.split(":", 1)
            data[tf] = loader.load_csv(path)
        result = engine.run(pair=args.pair, data_by_timeframe=data)
    else:
        result = engine.run_from_broker(
            pair=args.pair,
            platform=args.platform,
            bars=args.bars,
        )

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

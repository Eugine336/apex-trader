"""
APEX TRADER — Backtest Engine
Replays historical candles candle-by-candle so every decision can be stress-tested
before real capital is exposed.
"""

import asyncio
import os
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from brain.mtf_orchestrator import MTFOrchestrator, TradeSetup
from brain.trade_journal import TradeJournal, TradeRecord
from loguru import logger


@dataclass
class BacktestResult:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    max_drawdown: float
    max_consecutive_losses: int
    expectancy: float
    avg_hold_time: float
    best_pair: Optional[str]
    worst_pair: Optional[str]
    best_session: Optional[str]
    equity_curve: list[float]
    total_commission: float = 0.0
    total_slippage_cost: float = 0.0
    gross_profit_factor: float = 0.0
    net_profit_factor: float = 0.0


class DataLoader:
    """
    Loads historical OHLCV files from CSV and normalizes expected columns.
    """

    REQUIRED_COLUMNS = {"time", "open", "high", "low", "close"}

    def load_csv(self, file_path: str) -> pd.DataFrame:
        df = pd.read_csv(file_path)
        missing = self.REQUIRED_COLUMNS.difference(df.columns)
        if missing:
            raise ValueError(
                f"Missing required columns in {file_path}: {sorted(missing)}"
            )

        out = df.copy()
        out["time"] = pd.to_datetime(out["time"], utc=True)
        out = out.sort_values("time").reset_index(drop=True)
        return out

    def load_timeframe_map(self, files: dict[str, str]) -> dict[str, pd.DataFrame]:
        data: dict[str, pd.DataFrame] = {}
        for timeframe, file_path in files.items():
            data[timeframe] = self.load_csv(file_path)
        return data


class BrokerDataLoader:
    """
    Fetches historical OHLCV bars directly from connected broker platforms.
    Falls back to CSV if broker is unavailable.
    """

    MT5_TF_MAP = {
        "M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
        "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4",
        "D1": "TIMEFRAME_D1",
    }

    DERIV_GRANULARITY = {
        "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
        "H1": 3600, "H4": 14400, "D1": 86400,
    }

    def fetch_mt5(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 5000,
    ) -> pd.DataFrame:
        try:
            import MetaTrader5 as mt5

            login_env = os.getenv("MT5_LOGIN", "").strip()
            password = os.getenv("MT5_PASSWORD", "")
            server = os.getenv("MT5_SERVER", "")

            login = 0
            if login_env:
                try:
                    login = int(login_env)
                except ValueError:
                    logger.warning(f"Invalid MT5_LOGIN value: {login_env}")

            initialized = False
            if login and password and server:
                initialized = mt5.initialize(
                    login=login, password=password, server=server
                )
            if not initialized:
                initialized = mt5.initialize()
            if not initialized:
                raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

            if login and password and server:
                account = mt5.account_info()
                if account is None or account.login != login:
                    if not mt5.login(login, password=password, server=server):
                        raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")

            tf_const = getattr(mt5, self.MT5_TF_MAP[timeframe], None)
            if tf_const is None:
                raise ValueError(f"Unknown timeframe: {timeframe}")

            rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, bars)
            if rates is None or len(rates) == 0:
                raise RuntimeError(
                    f"MT5 returned no data for {symbol} {timeframe}: "
                    f"{mt5.last_error()}"
                )

            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.rename(columns={"tick_volume": "volume"})
            df = df[["time", "open", "high", "low", "close", "volume"]]
            df = df.sort_values("time").reset_index(drop=True)
            logger.info(f"MT5: fetched {len(df)} bars for {symbol} {timeframe}")
            return df

        except ImportError:
            raise RuntimeError("MetaTrader5 package not installed. Run: pip install MetaTrader5")

    def fetch_deriv(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 5000,
        api_token: str = "",
        app_id: str = "",
    ) -> pd.DataFrame:
        import json as _json
        import websockets

        from brain.symbol_mapper import SymbolMapper
        mapper = SymbolMapper("deriv")
        deriv_symbol = mapper.to_broker(symbol)

        granularity = self.DERIV_GRANULARITY.get(timeframe)
        if granularity is None:
            raise ValueError(f"Unsupported Deriv timeframe: {timeframe}")

        async def _fetch() -> list:
            url = f"wss://ws.binaryws.com/websockets/v3?app_id={app_id or '1089'}"
            async with websockets.connect(url) as ws:
                if api_token:
                    await ws.send(_json.dumps({"authorize": api_token}))
                    auth = _json.loads(await ws.recv())
                    if "error" in auth:
                        raise RuntimeError(f"Deriv auth failed: {auth['error']}")

                req = {
                    "ticks_history": deriv_symbol,
                    "end": "latest",
                    "count": bars,
                    "granularity": granularity,
                    "style": "candles",
                }
                await ws.send(_json.dumps(req))
                resp = _json.loads(await ws.recv())
                if "error" in resp:
                    raise RuntimeError(
                        f"Deriv error for {deriv_symbol}: {resp['error'].get('message')}"
                    )
                return resp.get("candles", [])

        candles = asyncio.run(_fetch())
        if not candles:
            raise RuntimeError(f"Deriv returned no candles for {deriv_symbol} {timeframe}")

        rows = [
            {
                "time": pd.Timestamp(c["epoch"], unit="s", tz="UTC"),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": 0.0,
            }
            for c in candles
        ]
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        logger.info(f"Deriv: fetched {len(df)} bars for {deriv_symbol} {timeframe}")
        return df

    def fetch_all_timeframes(
        self,
        symbol: str,
        timeframes: list[str],
        platform: str = "mt5",
        bars: int = 5000,
        **kwargs,
    ) -> dict[str, pd.DataFrame]:
        data: dict[str, pd.DataFrame] = {}
        for tf in timeframes:
            if platform == "mt5":
                data[tf] = self.fetch_mt5(symbol, tf, bars)
            elif platform == "deriv":
                data[tf] = self.fetch_deriv(symbol, tf, bars, **kwargs)
            else:
                raise ValueError(f"Unknown platform: {platform}")
        return data


class BacktestEngine:
    """
    Simulates setup generation and execution management over historical data.
    """

    def __init__(
        self,
        orchestrator: Optional[MTFOrchestrator] = None,
        journal: Optional[TradeJournal] = None,
        starting_balance: float = 10_000.0,
        risk_per_trade: float = 0.02,
        min_history: int = 120,
        pip_size: float = 0.0001,
        slippage_pips: float = 1.0,
        commission_per_lot: float = 3.5,
        broker_loader: Optional[BrokerDataLoader] = None,
    ):
        self.orchestrator = orchestrator or MTFOrchestrator(
            min_entry_score=65, pip_size=pip_size,
        )
        self.journal = journal
        self.starting_balance = starting_balance
        self.risk_per_trade = risk_per_trade
        self.min_history = min_history
        self.pip_size = pip_size
        self.slippage_pips = slippage_pips
        self.commission_per_lot = commission_per_lot
        self.broker_loader = broker_loader or BrokerDataLoader()

    def run(
        self,
        pair: str,
        data_by_timeframe: dict[str, pd.DataFrame],
        start_index: Optional[int] = None,
        end_index: Optional[int] = None,
    ) -> BacktestResult:
        if "M1" not in data_by_timeframe:
            raise ValueError("M1 timeframe is required for replay execution")

        m1 = data_by_timeframe["M1"].sort_values("time").reset_index(drop=True)
        start_index = start_index if start_index is not None else self.min_history
        end_index = end_index if end_index is not None else len(m1) - 1

        balance = self.starting_balance
        equity_curve = [balance]
        trade_returns_r: list[float] = []
        trade_returns_gross: list[float] = []
        hold_times: list[float] = []
        sessions: dict[str, list[float]] = {}
        open_trade: Optional[dict] = None
        consecutive_losses = 0
        max_consecutive_losses = 0
        total_commission = 0.0
        total_slippage_cost = 0.0

        for i in range(start_index, end_index + 1):
            candle = m1.iloc[i]
            now = pd.Timestamp(candle["time"]).to_pydatetime()

            slices = self._build_slices(data_by_timeframe, now)
            if not slices:
                continue

            if open_trade is None:
                setup = self.orchestrator.build_setup(
                    pair=pair, data_by_timeframe=slices, utc_now=now
                )
                if setup:
                    open_trade = self._open_trade(setup, now)
                continue

            close_event = self._evaluate_trade(open_trade, candle)
            if close_event is None:
                continue

            pnl_r = close_event["pnl_r"]
            trade_returns_gross.append(pnl_r)
            commission_cost = self.commission_per_lot / balance if balance > 0 else 0
            total_commission += self.commission_per_lot
            total_slippage_cost += open_trade.get("slippage_cost", 0.0)
            pnl_pct = pnl_r * self.risk_per_trade - commission_cost
            balance *= max(1.0 + pnl_pct, 0.01)
            equity_curve.append(balance)
            trade_returns_r.append(pnl_r)
            hold_times.append(close_event["hold_minutes"])

            session = open_trade["session"]
            sessions.setdefault(session, []).append(pnl_r)

            if pnl_r <= 0:
                consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
            else:
                consecutive_losses = 0

            self._journal_trade(pair, open_trade, close_event, now)
            open_trade = None

        if open_trade:
            final_candle = m1.iloc[end_index]
            forced_close = self._force_close(open_trade, final_candle)
            pnl_r = forced_close["pnl_r"]
            pnl_pct = pnl_r * self.risk_per_trade
            balance *= max(1.0 + pnl_pct, 0.01)
            equity_curve.append(balance)
            trade_returns_r.append(pnl_r)
            hold_times.append(forced_close["hold_minutes"])
            sessions.setdefault(open_trade["session"], []).append(pnl_r)
            self._journal_trade(
                pair,
                open_trade,
                forced_close,
                pd.Timestamp(final_candle["time"]).to_pydatetime(),
            )

        wins = sum(1 for r in trade_returns_r if r > 0)
        losses = sum(1 for r in trade_returns_r if r <= 0)
        total = len(trade_returns_r)
        win_rate = (wins / total * 100.0) if total else 0.0
        profit_factor = self._profit_factor(trade_returns_r)
        gross_pf = self._profit_factor(trade_returns_gross)
        sharpe = self._sharpe_ratio(trade_returns_r)
        max_dd = self._max_drawdown_pct(equity_curve)
        expectancy = float(np.mean(trade_returns_r)) if trade_returns_r else 0.0
        avg_hold = float(np.mean(hold_times)) if hold_times else 0.0
        best_session = self._best_session(sessions)

        return BacktestResult(
            total_trades=total,
            wins=wins,
            losses=losses,
            win_rate=round(win_rate, 2),
            profit_factor=round(profit_factor, 4)
            if np.isfinite(profit_factor)
            else float("inf"),
            sharpe_ratio=round(sharpe, 4),
            max_drawdown=round(max_dd, 4),
            max_consecutive_losses=max_consecutive_losses,
            expectancy=round(expectancy, 4),
            avg_hold_time=round(avg_hold, 2),
            best_pair=pair if total else None,
            worst_pair=pair if total else None,
            best_session=best_session,
            equity_curve=[round(v, 4) for v in equity_curve],
            total_commission=round(total_commission, 2),
            total_slippage_cost=round(total_slippage_cost, 6),
            gross_profit_factor=round(gross_pf, 4) if np.isfinite(gross_pf) else float("inf"),
            net_profit_factor=round(profit_factor, 4) if np.isfinite(profit_factor) else float("inf"),
        )

    def walk_forward(
        self,
        pair: str,
        data_by_timeframe: dict[str, pd.DataFrame],
        train_ratio: float = 0.7,
    ) -> dict[str, BacktestResult]:
        m1 = data_by_timeframe["M1"].sort_values("time").reset_index(drop=True)
        split = int(len(m1) * train_ratio)

        train = self.run(
            pair,
            data_by_timeframe,
            start_index=self.min_history,
            end_index=max(split - 1, self.min_history),
        )
        test = self.run(
            pair,
            data_by_timeframe,
            start_index=max(split, self.min_history),
            end_index=len(m1) - 1,
        )

        return {"train": train, "test": test}

    def run_from_broker(
        self,
        pair: str,
        platform: str = "mt5",
        timeframes: list[str] = None,
        bars: int = 5000,
        **kwargs,
    ) -> BacktestResult:
        timeframes = timeframes or ["H4", "H1", "M15", "M5", "M1"]
        logger.info(f"Fetching {bars} bars for {pair} from {platform}…")
        data = self.broker_loader.fetch_all_timeframes(
            symbol=pair,
            timeframes=timeframes,
            platform=platform,
            bars=bars,
            **kwargs,
        )
        return self.run(pair=pair, data_by_timeframe=data)

    def monte_carlo(
        self, trade_returns_r: list[float], iterations: int = 500
    ) -> dict[str, float]:
        if not trade_returns_r:
            return {
                "iterations": float(iterations),
                "worst_case_drawdown": 0.0,
                "median_total_r": 0.0,
                "p10_total_r": 0.0,
            }

        drawdowns = []
        totals = []
        for _ in range(iterations):
            sample = trade_returns_r.copy()
            random.shuffle(sample)
            equity = np.cumsum(sample)
            peaks = np.maximum.accumulate(equity)
            dd = float(np.max(peaks - equity)) if len(equity) else 0.0
            drawdowns.append(dd)
            totals.append(float(np.sum(sample)))

        return {
            "iterations": float(iterations),
            "worst_case_drawdown": round(max(drawdowns), 4),
            "median_total_r": round(float(np.median(totals)), 4),
            "p10_total_r": round(float(np.percentile(totals, 10)), 4),
        }

    def _build_slices(
        self,
        data_by_timeframe: dict[str, pd.DataFrame],
        now: datetime,
    ) -> Optional[dict[str, pd.DataFrame]]:
        required = ("H4", "H1", "M15", "M5", "M1")
        slices: dict[str, pd.DataFrame] = {}
        for tf in required:
            if tf not in data_by_timeframe:
                return None
            df = data_by_timeframe[tf]
            view = df[df["time"] <= now].copy()
            if len(view) < 25:
                return None
            slices[tf] = view.reset_index(drop=True)
        return slices

    def _open_trade(self, setup: TradeSetup, now: datetime) -> dict:
        slippage_distance = self.slippage_pips * self.pip_size
        if setup.direction == "LONG":
            actual_entry = setup.entry_price + slippage_distance
        else:
            actual_entry = setup.entry_price - slippage_distance

        risk = abs(actual_entry - setup.stop_loss)
        if risk <= 0:
            risk = 8 * self.pip_size

        return {
            "setup": setup,
            "entry_time": now,
            "entry_price": actual_entry,
            "stop_loss": setup.stop_loss,
            "tp1": setup.tp1,
            "tp2": setup.tp2,
            "risk": risk,
            "tp1_hit": False,
            "realized_r": 0.0,
            "session": self.orchestrator.session_engine.get_status(now).current_session,
            "entry_type": self._resolve_entry_type(setup),
            "slippage_cost": slippage_distance,
        }

    def _evaluate_trade(self, trade: dict, candle: pd.Series) -> Optional[dict]:
        direction = trade["setup"].direction
        entry = trade["entry_price"]
        stop = trade["stop_loss"]
        tp1 = trade["tp1"]
        tp2 = trade["tp2"]
        risk = trade["risk"]
        hold_minutes = (
            pd.Timestamp(candle["time"]).to_pydatetime() - trade["entry_time"]
        ).total_seconds() / 60.0

        if direction == "LONG":
            low_hit = candle["low"] <= stop
            tp1_hit = candle["high"] >= tp1
            tp2_hit = candle["high"] >= tp2

            if not trade["tp1_hit"]:
                if low_hit:
                    return {
                        "pnl_r": -1.0,
                        "hold_minutes": hold_minutes,
                        "outcome": "LOSS",
                    }
                if tp1_hit:
                    trade["tp1_hit"] = True
                    trade["stop_loss"] = entry
                    trade["realized_r"] += 0.5 * ((tp1 - entry) / risk)

            if trade["tp1_hit"]:
                stop_be_hit = candle["low"] <= trade["stop_loss"]
                if tp2_hit:
                    pnl = trade["realized_r"] + 0.5 * ((tp2 - entry) / risk)
                    return {
                        "pnl_r": pnl,
                        "hold_minutes": hold_minutes,
                        "outcome": "WIN",
                    }
                if stop_be_hit:
                    return {
                        "pnl_r": trade["realized_r"],
                        "hold_minutes": hold_minutes,
                        "outcome": "BREAKEVEN",
                    }

        else:
            high_hit = candle["high"] >= stop
            tp1_hit = candle["low"] <= tp1
            tp2_hit = candle["low"] <= tp2

            if not trade["tp1_hit"]:
                if high_hit:
                    return {
                        "pnl_r": -1.0,
                        "hold_minutes": hold_minutes,
                        "outcome": "LOSS",
                    }
                if tp1_hit:
                    trade["tp1_hit"] = True
                    trade["stop_loss"] = entry
                    trade["realized_r"] += 0.5 * ((entry - tp1) / risk)

            if trade["tp1_hit"]:
                stop_be_hit = candle["high"] >= trade["stop_loss"]
                if tp2_hit:
                    pnl = trade["realized_r"] + 0.5 * ((entry - tp2) / risk)
                    return {
                        "pnl_r": pnl,
                        "hold_minutes": hold_minutes,
                        "outcome": "WIN",
                    }
                if stop_be_hit:
                    return {
                        "pnl_r": trade["realized_r"],
                        "hold_minutes": hold_minutes,
                        "outcome": "BREAKEVEN",
                    }

        if hold_minutes >= 180:
            return self._time_close(trade, candle, hold_minutes)

        return None

    def _time_close(self, trade: dict, candle: pd.Series, hold_minutes: float) -> dict:
        direction = trade["setup"].direction
        entry = trade["entry_price"]
        close_price = float(candle["close"])
        risk = trade["risk"]
        if direction == "LONG":
            remaining = (close_price - entry) / risk
        else:
            remaining = (entry - close_price) / risk
        pnl = trade["realized_r"] + (0.5 * remaining if trade["tp1_hit"] else remaining)
        outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
        return {"pnl_r": pnl, "hold_minutes": hold_minutes, "outcome": outcome}

    def _force_close(self, trade: dict, candle: pd.Series) -> dict:
        hold_minutes = (
            pd.Timestamp(candle["time"]).to_pydatetime() - trade["entry_time"]
        ).total_seconds() / 60.0
        return self._time_close(trade, candle, hold_minutes)

    def _journal_trade(
        self, pair: str, trade: dict, close_event: dict, now: datetime
    ) -> None:
        if not self.journal:
            return

        setup = trade["setup"]
        record = TradeRecord(
            pair=pair,
            direction=setup.direction,
            entry=trade["entry_price"],
            exit=trade["entry_price"] + (close_event["pnl_r"] * trade["risk"]),
            pnl=close_event["pnl_r"],
            score=setup.score,
            confluences=[f"{c.name}:{c.score}" for c in setup.confluences],
            regime=setup.regime,
            session=trade["session"],
            spread=0.0,
            slippage=0.0,
            entry_type=trade["entry_type"],
            time_to_tp1=None,
            time_to_exit=close_event["hold_minutes"],
            outcome=close_event["outcome"],
            timestamp=now,
        )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.journal.log_trade(record))
        except RuntimeError:
            asyncio.run(self.journal.log_trade(record))

    def _resolve_entry_type(self, setup: TradeSetup) -> str:
        score_map = {c.name: c.score for c in setup.confluences}
        if score_map.get("Liquidity Sweep", 0) > 0:
            return "SWEEP"
        if score_map.get("FVG Zone", 0) > 0:
            return "FVG"
        if score_map.get("Order Block", 0) > 0:
            return "OB"
        return "MARKET"

    def _profit_factor(self, returns: list[float]) -> float:
        gains = sum(r for r in returns if r > 0)
        losses = sum(r for r in returns if r < 0)
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / abs(losses)

    def _sharpe_ratio(self, returns: list[float]) -> float:
        if len(returns) < 2:
            return 0.0
        std = float(np.std(returns, ddof=1))
        if std == 0:
            return 0.0
        return float(np.mean(returns) / std * np.sqrt(len(returns)))

    def _max_drawdown_pct(self, equity_curve: list[float]) -> float:
        if not equity_curve:
            return 0.0
        eq = np.array(equity_curve, dtype=float)
        peaks = np.maximum.accumulate(eq)
        dd = (peaks - eq) / np.maximum(peaks, 1e-9)
        return float(np.max(dd))

    def _best_session(self, sessions: dict[str, list[float]]) -> Optional[str]:
        if not sessions:
            return None
        ranked = sorted(
            sessions.items(), key=lambda item: np.mean(item[1]), reverse=True
        )
        return ranked[0][0]

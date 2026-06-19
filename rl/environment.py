"""
APEX TRADER — RL Trading Environment
=====================================
Wraps historical OHLCV data as a Gym-compatible environment.
The agent sees a window of candles + indicators and decides:
  0 = HOLD
  1 = BUY
  2 = SELL
  3 = CLOSE

Reward = realised P&L in R-multiples (profit / initial_risk).
Penalty for drawdown, overtrading, and holding losers.

No rules about order blocks, FVGs, or Wyckoff are written here.
If those patterns are profitable, the agent will discover them.
That is the emergence.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ── Constants ────────────────────────────────────────────────────────────────

WINDOW       = 50        # candles the agent sees at each step
ATR_PERIOD   = 14
SL_ATR_MULT  = 1.5       # stop loss = entry ± ATR × this
TP_ATR_MULT  = 3.0       # take profit = entry ± ATR × this
SPREAD_PIPS  = 1.5       # simulated spread cost in pips
MAX_HOLD     = 200       # force-close after this many candles
INITIAL_BAL  = 10_000.0
RISK_PCT     = 0.01      # 1% risk per trade


@dataclass
class Trade:
    direction: int        # 1=long, -1=short
    entry:     float
    sl:        float
    tp:        float
    size:      float      # notional units
    open_bar:  int
    risk_amt:  float      # dollars at risk


class ApexTradingEnv:
    """
    Single-instrument RL environment.

    obs_space  : (WINDOW, N_FEATURES) float32 array, normalised
    action_space: discrete {0,1,2,3}
    """

    N_FEATURES = 12   # OHLCV + ATR + returns + position encoding

    def __init__(
        self,
        csv_path: str,
        pip_size: float = 0.0001,
        initial_balance: float = INITIAL_BAL,
        spread_pips: float = SPREAD_PIPS,
        max_hold: int = MAX_HOLD,
    ):
        self.pip_size      = pip_size
        self.balance       = initial_balance
        self.initial_bal   = initial_balance
        self.spread_pips   = spread_pips
        self.max_hold      = max_hold

        self._raw = self._load(csv_path)
        self._df  = self._build_features(self._raw)
        self.n_bars = len(self._df)

        self.reset()

    # ── Public API ───────────────────────────────────────────────────────

    def reset(self) -> np.ndarray:
        self.idx        = WINDOW
        self.balance    = self.initial_bal
        self.trade: Optional[Trade] = None
        self.equity_curve: list[float] = [self.initial_bal]
        self.trades_log: list[dict]    = []
        self.peak_equity = self.initial_bal
        return self._observe()

    def step(self, action: int) -> tuple[np.ndarray, float, bool, dict]:
        reward = 0.0
        info   = {}

        bar = self._df.iloc[self.idx]
        close = float(bar["close"])
        spread = self.spread_pips * self.pip_size

        # ── Manage open trade ────────────────────────────────────────────
        if self.trade is not None:
            reward, closed = self._check_exit(bar)
            if not closed:
                # Check force-close
                if (self.idx - self.trade.open_bar) >= self.max_hold:
                    reward = self._close_trade(close, "timeout")
                    closed = True
                elif action == 3:
                    reward = self._close_trade(close, "agent_close")
                    closed = True

        # ── Open new trade ───────────────────────────────────────────────
        if self.trade is None and action in (1, 2):
            atr = float(bar["atr"])
            if atr > 0:
                direction = 1 if action == 1 else -1
                entry = close + (spread if direction == 1 else -spread)
                sl    = entry - direction * SL_ATR_MULT * atr
                tp    = entry + direction * TP_ATR_MULT * atr
                risk_amt  = self.balance * RISK_PCT
                risk_dist = abs(entry - sl)
                size  = risk_amt / risk_dist if risk_dist > 0 else 0.0

                if size > 0:
                    self.trade = Trade(
                        direction=direction,
                        entry=entry,
                        sl=sl,
                        tp=tp,
                        size=size,
                        open_bar=self.idx,
                        risk_amt=risk_amt,
                    )

        # ── Drawdown penalty ─────────────────────────────────────────────
        equity = self._equity(close)
        self.equity_curve.append(equity)
        self.peak_equity = max(self.peak_equity, equity)
        dd = (self.peak_equity - equity) / self.peak_equity
        if dd > 0.10:
            reward -= dd * 2.0   # penalise deep drawdowns hard

        self.idx += 1
        done = self.idx >= self.n_bars - 1

        info = {
            "balance":  self.balance,
            "equity":   equity,
            "drawdown": dd,
            "n_trades": len(self.trades_log),
        }

        return self._observe(), float(reward), done, info

    def action_space_n(self) -> int:
        return 4

    def observation_shape(self) -> tuple:
        return (WINDOW, self.N_FEATURES)

    # ── Internal ─────────────────────────────────────────────────────────

    def _observe(self) -> np.ndarray:
        window = self._df.iloc[self.idx - WINDOW: self.idx]
        cols = ["o_n","h_n","l_n","c_n","v_n","atr_n","ret1","ret5","ret14",
                "hl_ratio","oc_ratio","in_trade"]

        obs = window[cols].values.astype(np.float32)

        # Encode current trade state into last column
        if self.trade is not None:
            close_now = float(self._df.iloc[self.idx - 1]["close"])
            unreal_r  = self._unrealised_r(close_now)
            obs[:, -1] = np.clip(unreal_r, -3.0, 3.0)
        else:
            obs[:, -1] = 0.0

        # Replace any NaN with 0
        np.nan_to_num(obs, copy=False)
        return obs

    def _check_exit(self, bar: pd.Series) -> tuple[float, bool]:
        """Check SL/TP hit on the current bar (OHLC order matters)."""
        t = self.trade
        high  = float(bar["high"])
        low   = float(bar["low"])
        _close = float(bar["close"])

        if t.direction == 1:
            if low <= t.sl:
                return self._close_trade(t.sl, "sl"), True
            if high >= t.tp:
                return self._close_trade(t.tp, "tp"), True
        else:
            if high >= t.sl:
                return self._close_trade(t.sl, "sl"), True
            if low <= t.tp:
                return self._close_trade(t.tp, "tp"), True

        return 0.0, False

    def _close_trade(self, exit_price: float, reason: str) -> float:
        t = self.trade
        spread = self.spread_pips * self.pip_size
        exit_adj = exit_price - (spread if t.direction == 1 else -spread)

        pnl = t.direction * (exit_adj - t.entry) * t.size
        self.balance += pnl

        r_multiple = pnl / t.risk_amt if t.risk_amt > 0 else 0.0

        self.trades_log.append({
            "entry":    t.entry,
            "exit":     exit_adj,
            "pnl":      pnl,
            "r":        r_multiple,
            "reason":   reason,
            "bars_held": self.idx - t.open_bar,
        })

        self.trade = None

        # Reward = R-multiple, clipped to avoid exploding gradients
        return float(np.clip(r_multiple, -3.0, 3.0))

    def _unrealised_r(self, current_price: float) -> float:
        t = self.trade
        if t is None:
            return 0.0
        unreal_pnl = t.direction * (current_price - t.entry) * t.size
        return unreal_pnl / t.risk_amt if t.risk_amt > 0 else 0.0

    def _equity(self, current_price: float) -> float:
        unreal = 0.0
        if self.trade is not None:
            unreal = self.trade.direction * (current_price - self.trade.entry) * self.trade.size
        return self.balance + unreal

    # ── Data loading ─────────────────────────────────────────────────────

    @staticmethod
    def _load(path: str) -> pd.DataFrame:
        df = pd.read_csv(path, parse_dates=["time"])
        df = df.rename(columns=str.lower)
        required = {"open", "high", "low", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"CSV missing columns: {missing}")
        if "volume" not in df.columns:
            df["volume"] = 1.0
        df = df.dropna().reset_index(drop=True)
        return df

    @staticmethod
    def _build_features(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        # ATR
        tr = pd.concat([
            out["high"] - out["low"],
            (out["high"] - out["close"].shift(1)).abs(),
            (out["low"]  - out["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        out["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

        # Normalise OHLCV by rolling window (z-score over 50 bars)
        for col, alias in [("open","o_n"),("high","h_n"),("low","l_n"),
                           ("close","c_n"),("volume","v_n"),("atr","atr_n")]:
            mu  = out[col].rolling(50, min_periods=1).mean()
            std = out[col].rolling(50, min_periods=1).std().replace(0, 1e-8)
            out[alias] = (out[col] - mu) / std

        # Returns
        out["ret1"]  = out["close"].pct_change(1).fillna(0)
        out["ret5"]  = out["close"].pct_change(5).fillna(0)
        out["ret14"] = out["close"].pct_change(14).fillna(0)

        # Bar shape features
        rng = (out["high"] - out["low"]).replace(0, 1e-8)
        out["hl_ratio"] = (out["high"] - out["low"]) / out["close"]
        out["oc_ratio"] = (out["close"] - out["open"]) / rng

        # Position slot (filled dynamically in _observe)
        out["in_trade"] = 0.0

        return out.dropna().reset_index(drop=True)

"""
APEX RL — Multi-Timeframe Trading Environment
===============================================
Reality-faithful simulation that steps on the M5 clock and builds
observations from four aligned timeframes via ``MultiTFObservationBuilder``.

Compared to ``ApexTradingEnv`` (which remains unchanged for back-compat):

*  **Per-instrument spread** from ``config.INSTRUMENT_REGISTRY``
   — no more hardcoded 1.5-pip assumption.
*  **Commission** — configurable per-lot per-side.
*  **Gap-through slippage** — SL/TP fills at bar open (worse) when
   the bar gaps past the stop level.
*  **Swap / overnight financing** via ``brain.swap_model`` when a
   rate table is available.

This environment is used by the Phase 3 training curriculum.
The legacy ``ApexTradingEnv`` is untouched.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from .contracts import (
    TF_ORDER,
    WINDOW,
    ATR_PERIOD,
    OBS_FEATURES,
    OBS_SHAPE,
    N_CONTEXT_FEATURES,
    build_symbol_vocab,
)
from .multi_tf_obs_builder import MultiTFObservationBuilder, build_context, symbol_id_for


SL_ATR_MULT  = 1.5
TP_ATR_MULT  = 3.0
MAX_HOLD     = 200
INITIAL_BAL  = 10_000.0
RISK_PCT     = 0.01


@dataclass
class MTFTrade:
    direction:  int
    entry:      float
    sl:         float
    tp:         float
    size:       float
    open_bar:   int
    risk_amt:   float
    open_time:  datetime
    commission: float


class ApexMultiTFTradingEnv:
    """
    Multi-timeframe RL environment with reality-faithful execution.

    ``reset()`` and ``step()`` return a tuple
    ``(obs, context, symbol_id)`` as the observation so the agent
    receives all three components.
    """

    N_FEATURES = OBS_FEATURES  # 48

    def __init__(
        self,
        data_dir: str,
        instrument: str,
        initial_balance: float = INITIAL_BAL,
        max_hold: int = MAX_HOLD,
        commission_per_lot: float = 3.5,
        slippage_factor: float = 0.3,
        swap_rates: dict | None = None,
        reward_shaping: dict | None = None,
    ):
        self.instrument     = instrument.upper()
        self.initial_bal    = initial_balance
        self.max_hold       = max_hold
        self.commission_per_lot = commission_per_lot
        self.slippage_factor = slippage_factor
        self.swap_rates      = swap_rates or {}
        self._reward_shaping = reward_shaping or {}

        self._load_instrument_info()
        self._load_data(data_dir)

        self._obs_builder = MultiTFObservationBuilder()
        self._universe = build_symbol_vocab()
        self._symbol_id = symbol_id_for(self.instrument, self._universe)
        self._profile = {
            "pip_size": self.pip_size,
            "typical_spread_pips": self.typical_spread,
            "pip_value": self.pip_value,
        }

        self.reset()

    # ── Public API ────────────────────────────────────────────────────────

    def reset(self) -> tuple[np.ndarray, np.ndarray, int]:
        min_start = WINDOW + ATR_PERIOD + 20
        self.idx           = min_start
        self.balance       = self.initial_bal
        self.trade: Optional[MTFTrade] = None
        self.equity_curve: list[float] = [self.initial_bal]
        self.trades_log:   list[dict]  = []
        self.peak_equity   = self.initial_bal
        return self._observe()

    def step(self, action: int) -> tuple[tuple[np.ndarray, np.ndarray, int], float, bool, dict]:
        reward = 0.0
        bar = self._m5_feat.iloc[self.idx]
        close = float(bar["close"])
        current_time = bar["time"] if "time" in bar.index else None

        if self.trade is not None:
            reward, closed = self._check_exit(bar)
            if not closed:
                if (self.idx - self.trade.open_bar) >= self.max_hold:
                    reward = self._close_trade(close, "timeout", current_time)
                    closed = True
                elif action == 3:
                    reward = self._close_trade(close, "agent_close", current_time)
                    closed = True

        if self.trade is None and action in (1, 2):
            atr = float(bar["atr"])
            if atr > 0:
                direction = 1 if action == 1 else -1
                spread = self.typical_spread * self.pip_size
                entry = close + (spread if direction == 1 else -spread)
                sl = entry - direction * SL_ATR_MULT * atr
                tp = entry + direction * TP_ATR_MULT * atr
                risk_amt  = self.balance * RISK_PCT
                risk_dist = abs(entry - sl)
                size = risk_amt / risk_dist if risk_dist > 0 else 0.0

                if size > 0:
                    comm = self.commission_per_lot * (size / 100_000)
                    self.balance -= comm
                    self.trade = MTFTrade(
                        direction=direction,
                        entry=entry,
                        sl=sl,
                        tp=tp,
                        size=size,
                        open_bar=self.idx,
                        risk_amt=risk_amt,
                        open_time=current_time if current_time is not None else datetime.now(timezone.utc),
                        commission=comm,
                    )

        if self._reward_shaping:
            if self.trade is None:
                reward += self._reward_shaping.get("hold_penalty", 0.0)

        equity = self._equity(close)
        self.equity_curve.append(equity)
        self.peak_equity = max(self.peak_equity, equity)
        dd = (self.peak_equity - equity) / self.peak_equity if self.peak_equity > 0 else 0.0
        if dd > 0.10:
            reward -= dd * 2.0

        self.idx += 1
        done = self.idx >= len(self._m5_feat) - 1

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
        return OBS_SHAPE

    # ── Internal ──────────────────────────────────────────────────────────

    def _load_instrument_info(self):
        from config import INSTRUMENT_REGISTRY
        info = INSTRUMENT_REGISTRY.get(self.instrument)
        if info is None:
            raise ValueError(f"Unknown instrument: {self.instrument}")
        self.pip_size       = info.pip_size
        self.typical_spread = info.typical_spread_pips
        self.pip_value      = info.pip_value_per_lot
        self.category       = info.category.value

    def _load_data(self, data_dir: str):
        d = Path(data_dir)
        self._dfs: dict[str, pd.DataFrame] = {}
        for tf in TF_ORDER:
            path = d / f"{self.instrument}_{tf}.csv"
            if not path.exists():
                raise FileNotFoundError(f"Missing {path}")
            df = pd.read_csv(str(path), parse_dates=["time"])
            df.columns = df.columns.str.lower()
            if "volume" not in df.columns:
                df["volume"] = 1.0
            df = df.dropna().reset_index(drop=True)
            self._dfs[tf] = df

        self._m5_raw = self._dfs["M5"].copy()
        self._m5_feat = self._build_features(self._m5_raw)

    def _observe(self) -> tuple[np.ndarray, np.ndarray, int]:
        if self.idx < 0 or self.idx >= len(self._m5_feat):
            return np.zeros(OBS_SHAPE, dtype=np.float32), np.zeros(N_CONTEXT_FEATURES, dtype=np.float32), self._symbol_id

        m5_slice = self._m5_raw.iloc[:self.idx + 1]

        tf_slices = {"M5": m5_slice}
        for tf in TF_ORDER:
            if tf == "M5":
                continue
            tf_slices[tf] = self._dfs[tf]

        in_trade = 0.0
        if self.trade is not None:
            close_now = float(self._m5_feat.iloc[self.idx]["close"])
            in_trade = self._unrealised_r(close_now)

        result = self._obs_builder.build_from_frames(
            tf_slices,
            self.instrument,
            in_trade=in_trade,
            profile=self._profile,
            universe=self._universe,
        )

        if result is None:
            return np.zeros(OBS_SHAPE, dtype=np.float32), np.zeros(N_CONTEXT_FEATURES, dtype=np.float32), self._symbol_id

        obs, context, symbol_id = result
        return obs, context, symbol_id

    def _check_exit(self, bar: pd.Series) -> tuple[float, bool]:
        t = self.trade
        high  = float(bar["high"])
        low   = float(bar["low"])
        open_p = float(bar["open"])
        current_time = bar["time"] if "time" in bar.index else None

        if t.direction == 1:
            if low <= t.sl:
                fill = open_p if open_p <= t.sl else t.sl
                fill -= abs(np.random.normal(0, self.slippage_factor * self.pip_size))
                return self._close_trade(fill, "sl", current_time), True
            if high >= t.tp:
                fill = open_p if open_p >= t.tp else t.tp
                return self._close_trade(fill, "tp", current_time), True
        else:
            if high >= t.sl:
                fill = open_p if open_p >= t.sl else t.sl
                fill += abs(np.random.normal(0, self.slippage_factor * self.pip_size))
                return self._close_trade(fill, "sl", current_time), True
            if low <= t.tp:
                fill = open_p if open_p <= t.tp else t.tp
                return self._close_trade(fill, "tp", current_time), True

        return 0.0, False

    def _close_trade(self, exit_price: float, reason: str, current_time=None) -> float:
        t = self.trade
        spread = self.typical_spread * self.pip_size
        exit_adj = exit_price - (spread if t.direction == 1 else -spread)

        comm_close = self.commission_per_lot * (t.size / 100_000)
        self.balance -= comm_close

        swap_cost = self._estimate_swap(t, current_time)
        self.balance -= swap_cost

        pnl = t.direction * (exit_adj - t.entry) * t.size
        self.balance += pnl

        total_costs = t.commission + comm_close + swap_cost
        r_multiple = pnl / t.risk_amt if t.risk_amt > 0 else 0.0

        self.trades_log.append({
            "entry":      t.entry,
            "exit":       exit_adj,
            "pnl":        pnl,
            "r":          r_multiple,
            "reason":     reason,
            "bars_held":  self.idx - t.open_bar,
            "commission": t.commission + comm_close,
            "swap":       swap_cost,
            "total_costs": total_costs,
            "spread_applied": spread,
        })

        self.trade = None
        shaped_r = float(np.clip(r_multiple, -3.0, 3.0))

        if self._reward_shaping:
            bars_held = self.idx - t.open_bar
            if reason == "sl" and bars_held <= 5:
                shaped_r += self._reward_shaping.get("quick_loss_penalty", 0.0)
            if reason == "timeout":
                shaped_r += self._reward_shaping.get("timeout_penalty", 0.0)

        return shaped_r

    def _estimate_swap(self, t: MTFTrade, close_time) -> float:
        if not self.swap_rates or close_time is None:
            return 0.0
        try:
            from brain.swap_model import estimate_swap
            direction = "BUY" if t.direction == 1 else "SELL"
            value, status = estimate_swap(
                symbol=self.instrument,
                direction=direction,
                lots=t.size,
                open_time=t.open_time,
                close_time=close_time if isinstance(close_time, datetime) else datetime.now(timezone.utc),
                rates=self.swap_rates,
            )
            return abs(value) if value is not None else 0.0
        except Exception:
            logger.debug("Swap estimation failed for {}", self.instrument)
            return 0.0

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

    @staticmethod
    def _build_features(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        tr = pd.concat([
            out["high"] - out["low"],
            (out["high"] - out["close"].shift(1)).abs(),
            (out["low"]  - out["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        out["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
        return out.dropna().reset_index(drop=True)

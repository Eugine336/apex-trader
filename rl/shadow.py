"""
APEX RL — Shadow Trading Engine
================================
Stage 2 of the authority progression model.

The agent trained in simulation now runs against REAL market data
without sending any real orders.

Every signal is journaled.
Every outcome is tracked.
This is where false emergence gets exposed.

A behavior that worked in backtest but fails here is discarded.
A behavior that works here becomes a candidate for Stage 3.

Integration with APEX scanner:
  - ShadowEngine.get_signal(pair, obs) returns RLSignal
  - RLSignal contains: action, confidence, expected_r, latent_repr
  - scanner.py can consume this as an additional confluence factor
  - No trade authority. Journal only. Until shadow_score qualifies.
"""

from __future__ import annotations

import json
import sqlite3
import numpy as np
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .network import ApexRLAgent, ACTION_LABELS


# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class RLSignal:
    """
    The output of the RL subsystem at Stage 2/3.
    This is what gets fed to the APEX scanner as a confluence factor.
    """
    pair:           str
    timestamp:      str
    action:         int         # 0=HOLD, 1=BUY, 2=SELL, 3=CLOSE
    action_label:   str
    confidence:     float       # probability of chosen action (0-1)
    expected_r:     float       # estimated R-multiple from value head
    latent_repr:    list        # raw learned representation (for analysis)
    authority:      str         # "SHADOW" | "STAGE3" | "STAGE4" etc.


@dataclass
class ShadowTrade:
    """A paper trade opened by the shadow engine."""
    pair:       str
    direction:  int     # 1=long, -1=short
    entry:      float
    sl:         float
    tp:         float
    open_time:  str
    open_bar:   int
    expected_r: float
    confidence: float
    closed:     bool   = False
    exit:       float  = 0.0
    close_time: str    = ""
    actual_r:   float  = 0.0
    close_reason: str  = ""


# ── Shadow Engine ─────────────────────────────────────────────────────────────

class ShadowEngine:
    """
    Runs the trained RL agent against live/recent data in shadow mode.

    Usage:
        engine = ShadowEngine("checkpoints/apex_rl_best.pt")
        signal = engine.get_signal("EURUSD", obs_array)

        # In APEX scanner — add this alongside existing confluence score:
        if signal.action in (1, 2) and signal.confidence > 0.6:
            rl_confluence_boost = signal.expected_r * 0.1  # small weight at Stage 2
    """

    AUTHORITY = "SHADOW"

    # Thresholds for shadow score qualification (Stage 2 → Stage 3)
    MIN_SHADOW_TRADES    = 200
    MIN_WIN_RATE         = 0.52
    MIN_EXPECTANCY       = 0.3    # R-multiples
    MAX_DRAWDOWN         = 0.15
    MIN_REGIMES_TESTED   = 3

    def __init__(self, checkpoint_path: str, db_path: str = "shadow_journal.db"):
        self.agent: ApexRLAgent | None = None
        self._meta: dict = {}
        self._load(checkpoint_path)
        self.agent.eval()

        self.db_path = db_path
        self._init_db()

        self.open_trades: dict[str, ShadowTrade] = {}
        self.bar_counter: dict[str, int]          = {}

    # ── Signal generation ─────────────────────────────────────────────────

    def get_signal(
        self,
        pair: str,
        obs: np.ndarray,
        context_vec: Optional[np.ndarray] = None,
        symbol_id: Optional[int] = None,
    ) -> RLSignal:
        """
        Main interface. Call this from APEX scanner for each instrument.

        obs: numpy array of shape (WINDOW, N_FEATURES) — same format
             as the training environment observation.
        context_vec: optional instrument context vector (float32).
        symbol_id: optional integer symbol id for the embedding.
        """
        import torch
        with torch.no_grad():
            obs_t  = torch.FloatTensor(obs).unsqueeze(0)
            latent = self.agent.encoder(obs_t)
            action, conf, exp_r = self.agent.predict(
                obs, context_vec=context_vec, symbol_id=symbol_id,
            )
            latent_list = latent.squeeze(0).tolist()

        signal = RLSignal(
            pair=pair,
            timestamp=datetime.now(timezone.utc).isoformat(),
            action=action,
            action_label=ACTION_LABELS[action],
            confidence=round(conf, 4),
            expected_r=round(exp_r, 4),
            latent_repr=latent_list,
            authority=self.AUTHORITY,
        )

        self._log_signal(signal)
        return signal

    def update_price(
        self,
        pair:      str,
        high:      float,
        low:       float,
        close:     float,
        atr:       float,
        obs:       np.ndarray,
    ):
        """
        Call every bar to manage open shadow trades.
        Checks SL/TP/timeout, records outcomes.
        """
        self.bar_counter[pair] = self.bar_counter.get(pair, 0) + 1
        bar = self.bar_counter[pair]

        if pair not in self.open_trades:
            return

        t = self.open_trades[pair]
        if t.closed:
            return

        # Check SL/TP
        reason = None
        exit_p = None

        if t.direction == 1:
            if low <= t.sl:
                reason, exit_p = "sl", t.sl
            elif high >= t.tp:
                reason, exit_p = "tp", t.tp
        else:
            if high >= t.sl:
                reason, exit_p = "sl", t.sl
            elif low <= t.tp:
                reason, exit_p = "tp", t.tp

        # Timeout
        if reason is None and (bar - t.open_bar) >= 200:
            reason, exit_p = "timeout", close

        if reason:
            self._close_shadow_trade(pair, exit_p, reason)

    def open_shadow_trade(
        self,
        pair:       str,
        signal:     RLSignal,
        close:      float,
        atr:        float,
        pip_size:   float = 0.0001,
    ):
        """Open a paper trade based on a signal. Called by shadow coordinator."""
        if pair in self.open_trades and not self.open_trades[pair].closed:
            return  # already in a trade on this pair

        direction = 1 if signal.action == 1 else -1
        spread    = 1.5 * pip_size
        entry     = close + (spread if direction == 1 else -spread)
        sl        = entry - direction * 1.5 * atr
        tp        = entry + direction * 3.0 * atr

        t = ShadowTrade(
            pair=pair,
            direction=direction,
            entry=entry,
            sl=sl,
            tp=tp,
            open_time=datetime.now(timezone.utc).isoformat(),
            open_bar=self.bar_counter.get(pair, 0),
            expected_r=signal.expected_r,
            confidence=signal.confidence,
        )
        self.open_trades[pair] = t
        self._save_trade(t)

    # ── Qualification check ───────────────────────────────────────────────

    def shadow_score(self) -> dict:
        """
        Compute current shadow performance.
        Returns qualification status and detailed metrics.
        Used by authority progression manager.
        """
        trades = self._fetch_closed_trades()

        if len(trades) < self.MIN_SHADOW_TRADES:
            return {
                "qualified": False,
                "reason": f"Need {self.MIN_SHADOW_TRADES} trades, have {len(trades)}",
                "n_trades": len(trades),
            }

        r_values  = [t["actual_r"] for t in trades]
        wins      = [r for r in r_values if r > 0]
        losses    = [r for r in r_values if r <= 0]

        win_rate   = len(wins) / len(r_values)
        expectancy = np.mean(r_values)
        avg_win    = np.mean(wins)  if wins   else 0.0
        avg_loss   = np.mean(losses) if losses else 0.0

        # Equity curve drawdown
        equity     = np.cumsum(r_values)
        peak       = np.maximum.accumulate(equity)
        drawdown   = ((peak - equity) / (peak + 1e-8)).max()

        qualified = (
            win_rate   >= self.MIN_WIN_RATE    and
            expectancy >= self.MIN_EXPECTANCY  and
            drawdown   <= self.MAX_DRAWDOWN
        )

        return {
            "qualified":   qualified,
            "n_trades":    len(trades),
            "win_rate":    round(win_rate, 4),
            "expectancy":  round(float(expectancy), 4),
            "avg_win":     round(float(avg_win), 4),
            "avg_loss":    round(float(avg_loss), 4),
            "max_drawdown": round(float(drawdown), 4),
            "reason":      "qualified" if qualified else self._fail_reason(
                win_rate, expectancy, drawdown
            ),
        }

    def _fail_reason(self, wr, exp, dd) -> str:
        reasons = []
        if wr   < self.MIN_WIN_RATE:    reasons.append(f"win_rate {wr:.2f} < {self.MIN_WIN_RATE}")
        if exp  < self.MIN_EXPECTANCY:  reasons.append(f"expectancy {exp:.2f} < {self.MIN_EXPECTANCY}")
        if dd   > self.MAX_DRAWDOWN:    reasons.append(f"drawdown {dd:.2f} > {self.MAX_DRAWDOWN}")
        return "; ".join(reasons)

    # ── Database ──────────────────────────────────────────────────────────

    def _init_db(self):
        con = sqlite3.connect(self.db_path)
        con.execute("""
            CREATE TABLE IF NOT EXISTS shadow_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT, timestamp TEXT, action INTEGER,
                action_label TEXT, confidence REAL, expected_r REAL,
                authority TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS shadow_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT, direction INTEGER,
                entry REAL, sl REAL, tp REAL,
                open_time TEXT, expected_r REAL, confidence REAL,
                closed INTEGER DEFAULT 0,
                exit REAL, close_time TEXT,
                actual_r REAL, close_reason TEXT
            )
        """)
        con.commit()
        con.close()

    def _log_signal(self, s: RLSignal):
        con = sqlite3.connect(self.db_path)
        con.execute("""
            INSERT INTO shadow_signals
            (pair, timestamp, action, action_label, confidence, expected_r, authority)
            VALUES (?,?,?,?,?,?,?)
        """, (s.pair, s.timestamp, s.action, s.action_label,
              s.confidence, s.expected_r, s.authority))
        con.commit()
        con.close()

    def _save_trade(self, t: ShadowTrade):
        con = sqlite3.connect(self.db_path)
        con.execute("""
            INSERT INTO shadow_trades
            (pair, direction, entry, sl, tp, open_time, expected_r, confidence)
            VALUES (?,?,?,?,?,?,?,?)
        """, (t.pair, t.direction, t.entry, t.sl, t.tp,
              t.open_time, t.expected_r, t.confidence))
        con.commit()
        con.close()

    def _close_shadow_trade(self, pair: str, exit_price: float, reason: str):
        t = self.open_trades[pair]
        risk_dist = abs(t.entry - t.sl)
        actual_r  = t.direction * (exit_price - t.entry) / risk_dist if risk_dist > 0 else 0.0
        t.closed  = True
        t.exit    = exit_price
        t.close_time = datetime.now(timezone.utc).isoformat()
        t.actual_r   = round(actual_r, 4)
        t.close_reason = reason

        con = sqlite3.connect(self.db_path)
        con.execute("""
            UPDATE shadow_trades SET
                closed=1, exit=?, close_time=?, actual_r=?, close_reason=?
            WHERE pair=? AND closed=0
            ORDER BY id DESC LIMIT 1
        """, (exit_price, t.close_time, t.actual_r, reason, pair))
        con.commit()
        con.close()

    def _fetch_closed_trades(self) -> list[dict]:
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT actual_r FROM shadow_trades WHERE closed=1"
        ).fetchall()
        con.close()
        return [{"actual_r": r[0]} for r in rows]

    def _load(self, path: str):
        import torch
        from .contracts import assert_compatible, OBS_FEATURES, N_CONTEXT_FEATURES

        ckpt = torch.load(path, map_location="cpu", weights_only=True)
        meta = ckpt.get("meta", {})

        assert_compatible(meta)

        n_features = meta.get("n_features", 12)
        context_dim = meta.get("context_dim", 0)
        n_symbols = meta.get("n_symbols", 0)

        if n_features != OBS_FEATURES:
            raise ValueError(
                f"Checkpoint/production dimension mismatch: "
                f"checkpoint n_features={n_features}, "
                f"production contract OBS_FEATURES={OBS_FEATURES}. "
                f"See rl/contracts.py and docs/rl_obs_contract_decision.md "
                f"for resolution options."
            )
        if context_dim != N_CONTEXT_FEATURES:
            raise ValueError(
                f"Checkpoint/production context mismatch: "
                f"checkpoint context_dim={context_dim}, "
                f"production contract N_CONTEXT_FEATURES={N_CONTEXT_FEATURES}. "
                f"See rl/contracts.py and docs/rl_obs_contract_decision.md "
                f"for resolution options."
            )

        self.agent = ApexRLAgent(
            n_features=n_features,
            context_dim=context_dim,
            n_symbols=n_symbols,
        )
        self.agent.load_state_dict(ckpt["agent"])
        self._meta = meta

        print(
            f"[Shadow] Loaded checkpoint: step={ckpt.get('step', '?')}, "
            f"version={meta.get('obs_contract_version', '?')}, "
            f"features={n_features}, context={context_dim}"
        )

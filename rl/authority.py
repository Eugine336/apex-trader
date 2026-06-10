"""
APEX RL — Authority Progression Manager
=========================================
Enforces the 7-stage authority model from the vision document.

Stage 1: RL generates signals. No authority. Journal only.
Stage 2: RL generates predictions. Compared against scanner. No authority.
Stage 3: RL contributes confidence estimates. Scanner primary.
Stage 4: RL can influence trade ranking. Scanner primary.
Stage 5: RL can veto low-quality opportunities. Risk engine final.
Stage 6: Limited trade authority. Strict risk limits.
Stage 7: Expanded authority. Extensive validation required.

No stage promotion happens automatically without passing hard thresholds.
No RL component ever overrides the risk engine.
Demotion is automatic if performance degrades.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── Stage definitions ─────────────────────────────────────────────────────────

@dataclass
class StageRequirements:
    stage:              int
    label:              str
    min_shadow_trades:  int
    min_win_rate:       float
    min_expectancy:     float
    max_drawdown:       float
    min_live_trades:    int     # trades in controlled capital before next stage
    max_position_pct:   float   # max % of normal position size
    can_veto:           bool
    can_rank:           bool
    has_trade_auth:     bool


STAGES = [
    StageRequirements(
        stage=1, label="JOURNAL_ONLY",
        min_shadow_trades=0, min_win_rate=0.0, min_expectancy=0.0,
        max_drawdown=1.0, min_live_trades=0, max_position_pct=0.0,
        can_veto=False, can_rank=False, has_trade_auth=False,
    ),
    StageRequirements(
        stage=2, label="SHADOW_COMPARE",
        min_shadow_trades=50, min_win_rate=0.0, min_expectancy=0.0,
        max_drawdown=1.0, min_live_trades=0, max_position_pct=0.0,
        can_veto=False, can_rank=False, has_trade_auth=False,
    ),
    StageRequirements(
        stage=3, label="CONFIDENCE_SIGNAL",
        min_shadow_trades=200, min_win_rate=0.52, min_expectancy=0.25,
        max_drawdown=0.20, min_live_trades=0, max_position_pct=0.0,
        can_veto=False, can_rank=False, has_trade_auth=False,
    ),
    StageRequirements(
        stage=4, label="RANK_INFLUENCE",
        min_shadow_trades=500, min_win_rate=0.54, min_expectancy=0.35,
        max_drawdown=0.15, min_live_trades=0, max_position_pct=0.0,
        can_veto=False, can_rank=True, has_trade_auth=False,
    ),
    StageRequirements(
        stage=5, label="VETO_AUTHORITY",
        min_shadow_trades=1000, min_win_rate=0.56, min_expectancy=0.45,
        max_drawdown=0.12, min_live_trades=0, max_position_pct=0.0,
        can_veto=True, can_rank=True, has_trade_auth=False,
    ),
    StageRequirements(
        stage=6, label="LIMITED_TRADE_AUTH",
        min_shadow_trades=2000, min_win_rate=0.58, min_expectancy=0.55,
        max_drawdown=0.10, min_live_trades=50, max_position_pct=0.25,
        can_veto=True, can_rank=True, has_trade_auth=True,
    ),
    StageRequirements(
        stage=7, label="EXPANDED_AUTH",
        min_shadow_trades=5000, min_win_rate=0.60, min_expectancy=0.65,
        max_drawdown=0.08, min_live_trades=200, max_position_pct=1.0,
        can_veto=True, can_rank=True, has_trade_auth=True,
    ),
]

STAGE_MAP = {s.stage: s for s in STAGES}


# ── Performance snapshot ──────────────────────────────────────────────────────

@dataclass
class PerformanceSnapshot:
    timestamp:       str
    n_shadow_trades: int
    win_rate:        float
    expectancy:      float
    max_drawdown:    float
    n_live_trades:   int
    current_stage:   int
    qualified_for:   int
    notes:           str


# ── Authority Manager ─────────────────────────────────────────────────────────

class AuthorityManager:
    """
    Single source of truth for what the RL agent is allowed to do.

    Usage in APEX scanner:
        auth = AuthorityManager()
        perms = auth.get_permissions()

        if perms.can_rank:
            score = base_score + rl_signal.expected_r * perms.rank_weight
        if perms.can_veto and rl_signal.confidence < 0.3:
            skip_trade()
        if perms.has_trade_auth:
            size = normal_size * perms.max_position_pct
    """

    DEMOTION_DRAWDOWN_THRESHOLD = 0.25  # auto-demote if DD exceeds this
    DEMOTION_WINRATE_THRESHOLD  = 0.45  # auto-demote if win rate drops below this

    def __init__(self, db_path: str = "authority.db"):
        self.db_path = db_path
        self._init_db()          # creates tables + seeds row
        self._current_stage = self._load_stage()  # now safe to read

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def stage(self) -> int:
        return self._current_stage

    @property
    def stage_label(self) -> str:
        return STAGE_MAP[self._current_stage].label

    def get_permissions(self) -> "Permissions":
        s = STAGE_MAP[self._current_stage]
        return Permissions(
            stage=s.stage,
            label=s.label,
            can_veto=s.can_veto,
            can_rank=s.can_rank,
            has_trade_auth=s.has_trade_auth,
            max_position_pct=s.max_position_pct,
            rank_weight=self._rank_weight(s.stage),
        )

    def evaluate(self, metrics: dict) -> dict:
        """
        Feed current performance metrics.
        Returns promotion/demotion decision and reasoning.

        metrics keys expected:
            n_shadow_trades, win_rate, expectancy,
            max_drawdown, n_live_trades
        """
        result = {
            "current_stage": self._current_stage,
            "action": "HOLD",
            "reason": "",
            "new_stage": self._current_stage,
        }

        # ── Auto-demotion check ───────────────────────────────────────────
        if metrics.get("max_drawdown", 0) > self.DEMOTION_DRAWDOWN_THRESHOLD:
            if self._current_stage > 2:
                new_stage = max(2, self._current_stage - 2)
                self._set_stage(new_stage)
                result.update({
                    "action": "DEMOTED",
                    "reason": f"Drawdown {metrics['max_drawdown']:.2%} exceeded threshold",
                    "new_stage": new_stage,
                })
                self._log_snapshot(metrics, result["reason"])
                return result

        if metrics.get("win_rate", 1) < self.DEMOTION_WINRATE_THRESHOLD:
            if self._current_stage > 2:
                new_stage = max(2, self._current_stage - 1)
                self._set_stage(new_stage)
                result.update({
                    "action": "DEMOTED",
                    "reason": f"Win rate {metrics['win_rate']:.2%} below threshold",
                    "new_stage": new_stage,
                })
                self._log_snapshot(metrics, result["reason"])
                return result

        # ── Promotion check ───────────────────────────────────────────────
        next_stage = self._current_stage + 1
        if next_stage > 7:
            result["reason"] = "Already at maximum stage"
            return result

        req = STAGE_MAP[next_stage]
        passed, reason = self._check_requirements(metrics, req)

        if passed:
            self._set_stage(next_stage)
            result.update({
                "action": "PROMOTED",
                "reason": f"All requirements met for Stage {next_stage}",
                "new_stage": next_stage,
            })
        else:
            result.update({
                "action": "HOLD",
                "reason": f"Not yet qualified for Stage {next_stage}: {reason}",
                "new_stage": self._current_stage,
            })

        self._log_snapshot(metrics, result["reason"])
        return result

    def force_reset(self, reason: str = "manual reset"):
        """Emergency rollback to Stage 1."""
        self._set_stage(1)
        self._log_snapshot({}, f"FORCE RESET: {reason}")

    # ── Internal ─────────────────────────────────────────────────────────

    def _check_requirements(self, m: dict, req: StageRequirements) -> tuple[bool, str]:
        checks = [
            (m.get("n_shadow_trades", 0) >= req.min_shadow_trades,
             f"shadow_trades {m.get('n_shadow_trades',0)} < {req.min_shadow_trades}"),
            (m.get("win_rate", 0)    >= req.min_win_rate,
             f"win_rate {m.get('win_rate',0):.2%} < {req.min_win_rate:.2%}"),
            (m.get("expectancy", 0)  >= req.min_expectancy,
             f"expectancy {m.get('expectancy',0):.3f} < {req.min_expectancy}"),
            (m.get("max_drawdown", 1) <= req.max_drawdown,
             f"drawdown {m.get('max_drawdown',1):.2%} > {req.max_drawdown:.2%}"),
            (m.get("n_live_trades", 0) >= req.min_live_trades,
             f"live_trades {m.get('n_live_trades',0)} < {req.min_live_trades}"),
        ]
        failures = [msg for passed, msg in checks if not passed]
        return (len(failures) == 0), "; ".join(failures)

    def _rank_weight(self, stage: int) -> float:
        """How much the RL signal influences trade ranking at each stage."""
        weights = {1: 0.0, 2: 0.0, 3: 0.05, 4: 0.15, 5: 0.25, 6: 0.40, 7: 0.60}
        return weights.get(stage, 0.0)

    def _set_stage(self, stage: int):
        self._current_stage = stage
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE authority_state SET stage=?, updated=? WHERE id=1",
                    (stage, datetime.now(timezone.utc).isoformat()))
        con.commit()
        con.close()
        print(f"[Authority] Stage → {stage} ({STAGE_MAP[stage].label})")

    def _load_stage(self) -> int:
        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT stage FROM authority_state WHERE id=1").fetchone()
        con.close()
        return row[0] if row else 1

    def _log_snapshot(self, metrics: dict, notes: str):
        snap = PerformanceSnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
            n_shadow_trades=metrics.get("n_shadow_trades", 0),
            win_rate=metrics.get("win_rate", 0.0),
            expectancy=metrics.get("expectancy", 0.0),
            max_drawdown=metrics.get("max_drawdown", 0.0),
            n_live_trades=metrics.get("n_live_trades", 0),
            current_stage=self._current_stage,
            qualified_for=self._current_stage,
            notes=notes,
        )
        con = sqlite3.connect(self.db_path)
        con.execute("""
            INSERT INTO authority_log
            (timestamp, n_shadow_trades, win_rate, expectancy,
             max_drawdown, n_live_trades, current_stage, notes)
            VALUES (?,?,?,?,?,?,?,?)
        """, (snap.timestamp, snap.n_shadow_trades, snap.win_rate,
              snap.expectancy, snap.max_drawdown, snap.n_live_trades,
              snap.current_stage, snap.notes))
        con.commit()
        con.close()

    def _init_db(self):
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS authority_state (
                id INTEGER PRIMARY KEY,
                stage INTEGER DEFAULT 1,
                updated TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS authority_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, n_shadow_trades INTEGER,
                win_rate REAL, expectancy REAL, max_drawdown REAL,
                n_live_trades INTEGER, current_stage INTEGER, notes TEXT
            )
        """)
        # Seed state row if not exists
        con.execute("""
            INSERT OR IGNORE INTO authority_state (id, stage, updated)
            VALUES (1, 1, ?)
        """, (datetime.now(timezone.utc).isoformat(),))
        con.commit()
        con.close()

    def history(self) -> list[dict]:
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT * FROM authority_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        con.close()
        cols = ["id","timestamp","n_shadow_trades","win_rate","expectancy",
                "max_drawdown","n_live_trades","current_stage","notes"]
        return [dict(zip(cols, r)) for r in rows]


# ── Permissions object ────────────────────────────────────────────────────────

@dataclass
class Permissions:
    stage:            int
    label:            str
    can_veto:         bool
    can_rank:         bool
    has_trade_auth:   bool
    max_position_pct: float
    rank_weight:      float

    def __str__(self):
        return (
            f"Stage {self.stage} ({self.label}) | "
            f"veto={self.can_veto} rank={self.can_rank} "
            f"trade_auth={self.has_trade_auth} "
            f"pos_pct={self.max_position_pct:.0%} "
            f"rank_weight={self.rank_weight:.0%}"
        )

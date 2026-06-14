"""
Decision Journal — JSONL logging for every management decision.
Each line is a self-contained JSON record with full context, situation
dimensions, and reasoning.  Future training data for RL.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from decision.actions import EntryDecision, ManagementDecision
from decision.context import EntryContext, TradeContext
from decision.situation import SituationAssessment


class DecisionJournal:
    """Writes one JSONL line per decision to data/decision_journal/."""

    def __init__(self, base_dir: str = "data/decision_journal"):
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._current_date: str = ""
        self._file = None

    def log(
        self,
        ctx: TradeContext,
        sa: SituationAssessment,
        decision: ManagementDecision,
        governor_changed: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        record = {
            "timestamp": now.isoformat(),
            "symbol": ctx.symbol,
            "order_id": ctx.order_id,
            "direction": ctx.direction,
            "entry_type": ctx.entry_type,
            "hold_minutes": round(ctx.hold_minutes, 1),
            "pnl_pips": round(ctx.pnl_pips, 2),
            "pnl_dollars": round(ctx.pnl_dollars, 2),
            "profit_r": round(ctx.profit_r, 2),
            "scan_score": ctx.scan_score,
            "scan_direction": ctx.scan_direction,
            "situation": {
                "label": sa.primary_label,
                "tf_alignment": round(sa.tf_alignment, 3),
                "momentum": round(sa.momentum, 3),
                "structure_integrity": round(sa.structure_integrity, 3),
                "profit_state": round(sa.profit_state, 2),
                "urgency": round(sa.urgency, 3),
                "maturity": round(sa.maturity, 3),
                "read_confidence": round(sa.read_confidence, 3),
                "evidence": sa.evidence,
            },
            "context": {
                "d1_trend": ctx.d1_trend,
                "d1_confidence": round(ctx.d1_confidence, 2),
                "h4_trend": ctx.h4_trend,
                "h4_confidence": round(ctx.h4_confidence, 2),
                "h1_trend": ctx.h1_trend,
                "h1_confidence": round(ctx.h1_confidence, 2),
                "m1_trend": ctx.m1_trend,
                "m1_event": ctx.m1_event,
                "m1_aligned": ctx.m1_aligned_count,
                "at_breakeven": ctx.at_breakeven,
                "tp1_hit": ctx.tp1_hit,
                "trailing": ctx.trailing,
                "session": ctx.session_name,
                "portfolio_heat_pct": round(ctx.portfolio_heat_pct, 2),
                "context_pressure": ctx.context_pressure,
            },
            "decision": {
                "action": decision.action.value,
                "reason": decision.reason,
                "confidence": round(decision.confidence, 3),
                "governor_changed": governor_changed,
                "new_sl": decision.new_sl,
                "evidence": decision.evidence,
            },
        }

        try:
            date_str = now.strftime("%Y-%m-%d")
            if date_str != self._current_date:
                self._rotate_file(date_str)
            if self._file is not None:
                self._file.write(json.dumps(record, default=str) + "\n")
                self._file.flush()
        except Exception as exc:
            logger.warning("[DecisionJournal] write failed: {}", exc)

        logger.info(
            "[DECISION] {} {} | {} → {} | {} | pnl=${:+.2f} ({:+.1f}pip) align={:+.2f} struct={:.2f} | {}",
            ctx.direction, ctx.symbol,
            sa.primary_label, decision.action.value,
            "GOVERNOR" if governor_changed else "ENGINE",
            ctx.pnl_dollars, ctx.pnl_pips, sa.tf_alignment, sa.structure_integrity,
            decision.reason[:120],
        )

    def log_entry(
        self,
        ctx: EntryContext,
        sa: SituationAssessment,
        decision: EntryDecision,
        governor_changed: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        record = {
            "timestamp": now.isoformat(),
            "decision_type": "ENTRY",
            "symbol": ctx.symbol,
            "direction": ctx.direction,
            "scan_score": ctx.scan_score,
            "entry_type": ctx.entry_type,
            "entry_price": ctx.entry_price,
            "stop_loss": ctx.stop_loss,
            "risk_reward_2": round(ctx.risk_reward_2, 2),
            "risk_pips": round(ctx.risk_pips, 2),
            "regime": ctx.regime,
            "situation": {
                "label": sa.primary_label,
                "tf_alignment": round(sa.tf_alignment, 3),
                "momentum": round(sa.momentum, 3),
                "structure_integrity": round(sa.structure_integrity, 3),
                "urgency": round(sa.urgency, 3),
                "read_confidence": round(sa.read_confidence, 3),
                "evidence": sa.evidence,
            },
            "context": {
                "d1_trend": ctx.d1_trend,
                "d1_confidence": round(ctx.d1_confidence, 2),
                "h4_trend": ctx.h4_trend,
                "h4_confidence": round(ctx.h4_confidence, 2),
                "h1_trend": ctx.h1_trend,
                "h1_confidence": round(ctx.h1_confidence, 2),
                "m1_trend": ctx.m1_trend,
                "m1_event": ctx.m1_event,
                "m1_aligned": ctx.m1_aligned_count,
                "session": ctx.session_name,
                "portfolio_heat_pct": round(ctx.portfolio_heat_pct, 2),
                "spread": round(ctx.current_spread, 2),
                "typical_spread": round(ctx.typical_spread, 2),
                "ev_estimate": round(ctx.ev_estimate, 4),
                "pair_multiplier": round(ctx.pair_multiplier, 2),
            },
            "decision": {
                "action": decision.action.value,
                "reason": decision.reason,
                "confidence": round(decision.confidence, 3),
                "conviction": round(decision.conviction, 3),
                "size_multiplier": round(decision.size_multiplier, 2),
                "governor_changed": governor_changed,
                "governor_vetoed": decision.governor_vetoed,
                "governor_reason": decision.governor_reason,
                "evidence": decision.evidence,
            },
        }

        try:
            date_str = now.strftime("%Y-%m-%d")
            if date_str != self._current_date:
                self._rotate_file(date_str)
            if self._file is not None:
                self._file.write(json.dumps(record, default=str) + "\n")
                self._file.flush()
        except Exception as exc:
            logger.warning("[DecisionJournal] entry write failed: {}", exc)

        logger.info(
            "[ENTRY DECISION] {} {} | {} → {} | {} | score={} align={:+.2f} struct={:.2f} conv={:.2f} | {}",
            ctx.direction, ctx.symbol,
            sa.primary_label, decision.action.value,
            "GOVERNOR" if governor_changed else "ENGINE",
            ctx.scan_score, sa.tf_alignment, sa.structure_integrity,
            decision.conviction,
            decision.reason[:120],
        )

    def _rotate_file(self, date_str: str) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
        filepath = self._base_dir / f"decisions_{date_str}.jsonl"
        self._file = open(filepath, "a", encoding="utf-8")
        self._current_date = date_str

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

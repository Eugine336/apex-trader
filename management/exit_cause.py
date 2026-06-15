"""
APEX TRADER — Exit Cause taxonomy.

A trade can leave the book through many different paths (mechanical TP/SL,
strategic thesis decay, portfolio-heat trims, survival flattens, news/session
guards, …).  Historically the *why* survived only as a free-text ``outcome``
string, so the learners could not distinguish "stopped at breakeven" from
"thesis decay" from "news exit" as a feature.

``ExitCause`` is the normalised enum every exit path is tagged with **at the
source** (the exit decision site), not parsed back out of a string after the
fact.  ``from_reason`` exists only as a best-effort fallback for reasons that
genuinely originate as strings (broker-side closes, mechanical
``TradeManager.close_reason``) — never as the primary tagging mechanism.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class ExitCause(Enum):
    """Normalised reason a position left the book — a learning feature."""

    # ── Mechanical target / stop exits (TradeManager) ─────────────────────
    TP1_PARTIAL = "tp1_partial"
    TP2_TARGET = "tp2_target"
    TP3_EXTENDED = "tp3_extended"
    STOP_LOSS = "stop_loss"
    BREAKEVEN_STOP = "breakeven_stop"
    TRAILING_STOP = "trailing_stop"
    STALL_EXIT = "stall_exit"
    STRUCTURE_EXIT = "structure_exit"

    # ── Strategic brain (DecisionEngine) ──────────────────────────────────
    THESIS_SECURE = "thesis_secure"
    THESIS_DECAY = "thesis_decay"
    STRATEGIC_CLOSE = "strategic_close"
    FAST_OPPOSITION_DECAY = "fast_opposition_decay"

    # ── Protective / guard exits ──────────────────────────────────────────
    NEWS_EXIT = "news_exit"
    SESSION_CLOSE = "session_close"
    SPREAD_DETERIORATION = "spread_deterioration"
    OPPORTUNITY_COST = "opportunity_cost"
    WEEKEND_PROTECTION = "weekend_protection"

    # ── Legacy active-management checks (C19–C22) ─────────────────────────
    INVALIDATION = "invalidation"
    CONVICTION_COLLAPSE = "conviction_collapse"
    HTF_CANDLE_CLOSE = "htf_candle_close"
    DYNAMIC_SL_TIGHTEN_EXIT = "dynamic_sl_tighten_exit"

    # ── Portfolio-heat / survival paths ───────────────────────────────────
    HEAT_TRIM = "heat_trim"
    HEAT_EMERGENCY = "heat_emergency"
    MARGIN_FLATTEN = "margin_flatten"
    ACCOUNT_FLATTEN = "account_flatten"

    # ── Out-of-band ───────────────────────────────────────────────────────
    BROKER_SIDE = "broker_side"
    MANUAL = "manual"
    UNKNOWN = "unknown"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @classmethod
    def from_reason(cls, reason: Optional[str]) -> "ExitCause":
        """Best-effort classification of a free-text close reason.

        Used ONLY as a fallback for reasons that genuinely arrive as strings
        (broker-side detection, mechanical ``close_reason``). Explicit exit
        sites should pass an ``ExitCause`` directly instead of relying on this.
        """
        if not reason:
            return cls.UNKNOWN
        r = reason.strip().lower()

        # Order matters: check the most specific markers first so a generic
        # substring (e.g. "stop") does not shadow "stopped at breakeven".
        if "breakeven" in r or "break even" in r or "be stop" in r:
            return cls.BREAKEVEN_STOP
        if "trail" in r:
            return cls.TRAILING_STOP
        if "stall" in r:
            return cls.STALL_EXIT
        if "structure" in r:
            return cls.STRUCTURE_EXIT
        if "tp1" in r:
            return cls.TP1_PARTIAL
        if "tp3" in r:
            return cls.TP3_EXTENDED
        if "tp2" in r or "take profit" in r or "tp hit" in r:
            return cls.TP2_TARGET
        if "stop loss" in r or "stop-loss" in r or "stopped out" in r or "sl hit" in r:
            return cls.STOP_LOSS
        # Strategic / thesis closes (DecisionEngine reasons).
        if "severe thesis" in r or "thesis collapse" in r or "deterioration" in r or "decay" in r:
            return cls.THESIS_DECAY
        if "thesis" in r and "secure" in r:
            return cls.THESIS_SECURE
        if "secure" in r:
            return cls.THESIS_SECURE
        if "fast cluster" in r or "fast-opposition" in r or "fast opposition" in r:
            return cls.FAST_OPPOSITION_DECAY
        if "opposing" in r or "invalidation" in r:
            return cls.INVALIDATION
        if "conviction" in r:
            return cls.CONVICTION_COLLAPSE
        if "htf" in r:
            return cls.HTF_CANDLE_CLOSE
        if "decision_engine" in r or "decision engine" in r:
            return cls.STRATEGIC_CLOSE
        if "news" in r:
            return cls.NEWS_EXIT
        if "session" in r:
            return cls.SESSION_CLOSE
        if "spread" in r:
            return cls.SPREAD_DETERIORATION
        if "opportunity" in r:
            return cls.OPPORTUNITY_COST
        if "weekend" in r:
            return cls.WEEKEND_PROTECTION
        if "emergency" in r:
            return cls.HEAT_EMERGENCY
        if "margin" in r:
            return cls.MARGIN_FLATTEN
        if "daily_loss" in r or "daily loss" in r or "account flatten" in r:
            return cls.ACCOUNT_FLATTEN
        if "external" in r or "offline" in r or "broker" in r:
            return cls.BROKER_SIDE
        return cls.UNKNOWN

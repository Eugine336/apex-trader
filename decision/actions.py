"""
Actions the decision engine can recommend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Action(Enum):
    HOLD = "HOLD"
    CLOSE = "CLOSE"
    TIGHTEN_SL = "TIGHTEN_SL"
    MOVE_TO_BREAKEVEN = "MOVE_TO_BREAKEVEN"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    SCALE_IN = "SCALE_IN"
    SET_PROTECTIVE_STOP = "SET_PROTECTIVE_STOP"
    OBSERVE = "OBSERVE"


@dataclass
class ManagementDecision:
    """What the decision engine recommends for an open trade."""
    action: Action
    reason: str
    confidence: float = 0.5       # 0.0–1.0
    new_sl: Optional[float] = None
    partial_ratio: float = 0.0
    scale_lots: float = 0.0
    evidence: list[str] = field(default_factory=list)
    # Normalised exit-cause tag (an ``ExitCause`` value string) set at the
    # decision source when a specific management behaviour drove the verdict —
    # e.g. fast-cluster opposition decay. Left None for the generic strategic
    # close so the executor falls back to its default cause mapping.
    exit_cause: Optional[str] = None

    @property
    def should_close(self) -> bool:
        return self.action == Action.CLOSE


class EntryAction(Enum):
    ENTER_MARKET = "ENTER_MARKET"
    ENTER_PENDING = "ENTER_PENDING"
    SKIP = "SKIP"


@dataclass
class EntryDecision:
    """What the decision engine recommends for a potential entry."""
    action: EntryAction
    reason: str
    confidence: float = 0.5
    conviction: float = 0.5       # 0.0–1.0, drives position sizing
    size_multiplier: float = 1.0  # conviction-derived lot scaling
    # Continuous enter-skip margin (enter_score − skip_score). Exposed so the
    # orchestrator can size by *how strongly* ENTER won, not just the binary —
    # a margin of 0.01 and 5.0 both mean ENTER but carry very different weight.
    entry_margin: float = 0.0
    evidence: list[str] = field(default_factory=list)
    governor_vetoed: bool = False
    governor_reason: str = ""

    @property
    def should_enter(self) -> bool:
        return self.action in (EntryAction.ENTER_MARKET, EntryAction.ENTER_PENDING)

    @property
    def is_market(self) -> bool:
        return self.action == EntryAction.ENTER_MARKET

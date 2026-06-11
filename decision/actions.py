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
    evidence: list[str] = field(default_factory=list)
    governor_vetoed: bool = False
    governor_reason: str = ""

    @property
    def should_enter(self) -> bool:
        return self.action in (EntryAction.ENTER_MARKET, EntryAction.ENTER_PENDING)

    @property
    def is_market(self) -> bool:
        return self.action == EntryAction.ENTER_MARKET

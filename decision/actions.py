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

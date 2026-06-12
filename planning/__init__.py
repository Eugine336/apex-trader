"""
APEX TRADER — Planning layer.

A coordinator that sits between the analysis advisors (scanner, decision
engine, RL, adaptive/ML) and execution.  It replaces the serial yes/no veto
chain with a single rich `TradePlan` that explains *what* to do and *why*.

  TradePlanContext  → all advisor analysis (no verdicts)
  TradePlanner      → produces a complete TradePlan
  OutcomeLogger     → records plan→outcome pairs for learning
  Calibrator        → evolves PlannerConfig from realised outcomes
"""

from planning.calibrator import Calibrator
from planning.models import TradePlan, TradePlanContext
from planning.outcome_logger import OutcomeLogger
from planning.trade_planner import PlannerConfig, TradePlanner

__all__ = [
    "TradePlan",
    "TradePlanContext",
    "TradePlanner",
    "PlannerConfig",
    "OutcomeLogger",
    "Calibrator",
]

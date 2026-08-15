"""
APEX TRADER — Planning layer.

A coordinator that sits between the analysis advisors (scanner, decision
engine, RL, adaptive/ML) and execution.  It replaces the serial yes/no veto
chain with a single rich `TradePlan` that explains *what* to do and *why*.

  TradePlanContext  → all advisor analysis (no verdicts)
  TradePlanner      → produces a complete TradePlan
  OutcomeLogger     → records plan→outcome pairs for learning
  Calibrator        → evolves PlannerConfig from realised outcomes

.. deprecated::
    ``TradePlanner`` is a legacy planning component slated for retirement as
    management/origination authority consolidates in the event-driven Cognitive
    Brain path. Constructing a ``TradePlanner`` emits a ``DeprecationWarning``;
    avoid new dependencies on it.
"""

from planning.calibrator import Calibrator
from planning.models import TradePlan, TradePlanContext
from planning.outcome_logger import OutcomeLogger
# Deprecated (legacy, slated for retirement): TradePlanner — construction emits
# a DeprecationWarning. See planning/trade_planner.py.
from planning.trade_planner import PlannerConfig, TradePlanner

__all__ = [
    "TradePlan",
    "TradePlanContext",
    "TradePlanner",
    "PlannerConfig",
    "OutcomeLogger",
    "Calibrator",
]

"""
APEX TRADER — Portfolio Governor layer.

Adds portfolio-level risk enforcement on top of the per-trade RiskEngine:
position caps, currency/sector concentration limits, correlated-exposure
limits, and a daily-loss-cap halt.  Advisory to the planner (defence in
depth) and fail-closed by default (configurable via GovernorConfig.fail_closed).
"""

from governor.models import GovernorConfig, GovernorVerdict
from governor.portfolio_governor import PortfolioGovernor

__all__ = [
    "GovernorConfig",
    "GovernorVerdict",
    "PortfolioGovernor",
]

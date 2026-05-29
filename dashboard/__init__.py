"""
APEX TRADER — Dashboard Package
Live monitoring, trade history, scanner view, risk status, and bot controls.
FastAPI backend serving real-time data from the trading engine.
"""

from dashboard.state import LiveState
from dashboard.api import create_app

__all__ = ["LiveState", "create_app"]

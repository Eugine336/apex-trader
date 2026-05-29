"""
APEX TRADER — Platform Integration Package
Two arms, one mind. MT5 for Forex/indices. Deriv for synthetics — 24/7.
"""

from platforms.base_connector import (
    AccountInfo,
    BaseConnector,
    CloseResult,
    OrderDirection,
    OrderResult,
    OrderStatus,
    PositionInfo,
    TickData,
)
from platforms.deriv.deriv_connector import DerivConnector
from platforms.main_loop import ManagedPosition, TradingLoop
from platforms.mt5.mt5_connector import MT5Connector
from platforms.platform_manager import PlatformManager

__all__ = [
    "BaseConnector",
    "AccountInfo",
    "TickData",
    "OrderResult",
    "CloseResult",
    "PositionInfo",
    "OrderDirection",
    "OrderStatus",
    "MT5Connector",
    "DerivConnector",
    "PlatformManager",
    "TradingLoop",
    "ManagedPosition",
]

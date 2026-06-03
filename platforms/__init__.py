"""
APEX TRADER — Platform Integration Package
Two arms, one mind. MT5 for Forex/indices. Deriv for synthetics — 24/7.
"""

import importlib

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

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "MT5Connector": ("platforms.mt5.mt5_connector", "MT5Connector"),
    "DerivConnector": ("platforms.deriv.deriv_connector", "DerivConnector"),
    "PlatformManager": ("platforms.platform_manager", "PlatformManager"),
    "TradingLoop": ("platforms.main_loop", "TradingLoop"),
    "ManagedPosition": ("platforms.main_loop", "ManagedPosition"),
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr = _LAZY_IMPORTS[name]
        mod = importlib.import_module(module_path)
        value = getattr(mod, attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module 'platforms' has no attribute {name!r}")

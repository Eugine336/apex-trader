"""
APEX TRADER — Risk Package
The Shield. Protects the account above everything else.

Exports are resolved lazily so importing dependency-free leaf modules such as
risk.owner_limits does not eagerly import RiskEngine -> config and create a
package initialization cycle.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "RiskEngine": ("risk.risk_engine", "RiskEngine"),
    "RiskAssessment": ("risk.risk_engine", "RiskAssessment"),
    "AccountSnapshot": ("risk.risk_engine", "AccountSnapshot"),
    "PositionSizer": ("risk.position_sizer", "PositionSizer"),
    "SizeResult": ("risk.position_sizer", "SizeResult"),
    "PnLTracker": ("risk.daily_tracker", "PnLTracker"),
    "PnLSnapshot": ("risk.daily_tracker", "PnLSnapshot"),
    "SpreadMonitor": ("risk.spread_monitor", "SpreadMonitor"),
    "SpreadReading": ("risk.spread_monitor", "SpreadReading"),
    "RiskReporter": ("risk.risk_reporter", "RiskReporter"),
    "RiskReport": ("risk.risk_reporter", "RiskReport"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value

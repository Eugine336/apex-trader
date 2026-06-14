"""
The execution-quality size throttle is live by default (demo validation).

It only ever REDUCES size on degraded execution (high slippage/latency/spread),
never increases it, so it is safe to run while the grade thresholds are tuned.
"""

from config import RiskConfig


def test_execution_quality_sizing_enabled_by_default():
    assert RiskConfig().execution_quality_sizing_enabled is True

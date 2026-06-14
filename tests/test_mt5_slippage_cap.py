"""
Test for #8 — per-instrument slippage cap (MT5 deviation).

`_deviation_points` converts the configured max-slippage (pips) into broker
points using each symbol's own point size, falling back to the legacy global
deviation when disabled or when the point size is unknown.
"""

from platforms.mt5.mt5_connector import MT5Connector


def test_default_uses_legacy_global_deviation():
    c = MT5Connector()  # max_slippage_pips defaults to 0.0 (disabled)
    assert c._deviation_points("EURUSD", 0.00001) == c._deviation == 20


def test_per_instrument_from_pips():
    c = MT5Connector(max_slippage_pips=2.0)
    # EURUSD pip_size 0.0001; 5-digit point 0.00001 → 10 points/pip → 2 pips = 20
    assert c._deviation_points("EURUSD", 0.00001) == 20
    # coarser point (1 point/pip) → 2 pips = 2 points
    assert c._deviation_points("EURUSD", 0.0001) == 2


def test_zero_or_unknown_point_falls_back():
    c = MT5Connector(max_slippage_pips=2.0)
    assert c._deviation_points("EURUSD", 0.0) == c._deviation

"""Tests for cognition.position_adapter — the live-book → Brain view mapping.

Regression coverage for the two coupled bugs that left the Brain unable to
manage its own positions:
  A. positions were sourced from the campaign registry (empty for Brain-
     originated trades), and
  B. the management sink compared LONG/SHORT against a broker BUY/SELL side and
     never matched — silently dropping every EXIT/PARTIAL/PROTECT/TIGHTEN.
Both hinge on a single canonical side axis, exercised here.
"""

from __future__ import annotations

import time

from cognition.position_adapter import (
    canonical_side,
    broker_profit_r,
    hold_seconds_from,
)


class TestCanonicalSide:
    def test_broker_buy_sell_maps_to_long_short(self):
        assert canonical_side("BUY") == "LONG"
        assert canonical_side("SELL") == "SHORT"

    def test_brain_long_short_passthrough(self):
        assert canonical_side("LONG") == "LONG"
        assert canonical_side("short") == "SHORT"

    def test_buy_matches_long_across_axes(self):
        # The exact match the management sink relies on.
        assert canonical_side("BUY") == canonical_side("LONG")
        assert canonical_side("SELL") == canonical_side("SHORT")

    def test_unknown_is_empty(self):
        for bad in ("", None, "flat", "xyz"):
            assert canonical_side(bad) == ""


class TestBrokerProfitR:
    def test_long_in_profit(self):
        # entry 100, sl 90 (risk 10), price 115 → +1.5R
        assert broker_profit_r("BUY", 100.0, 115.0, 90.0) == 1.5

    def test_long_underwater(self):
        assert broker_profit_r("LONG", 100.0, 95.0, 90.0) == -0.5

    def test_short_in_profit(self):
        # entry 100, sl 110 (risk 10), price 92 → +0.8R
        assert broker_profit_r("SELL", 100.0, 92.0, 110.0) == 0.8

    def test_missing_prices_returns_none(self):
        assert broker_profit_r("BUY", 0.0, 100.0, 90.0) is None
        assert broker_profit_r("BUY", 100.0, 0.0, 90.0) is None
        assert broker_profit_r("BUY", 100.0, 100.0, 0.0) is None

    def test_zero_risk_returns_none(self):
        assert broker_profit_r("BUY", 100.0, 105.0, 100.0) is None

    def test_unknown_side_returns_none(self):
        assert broker_profit_r("flat", 100.0, 115.0, 90.0) is None


class TestHoldSecondsFrom:
    class _DT:
        def __init__(self, ts):
            self._ts = ts

        def timestamp(self):
            return self._ts

    def test_epoch_seconds(self):
        h = hold_seconds_from(time.time() - 120.0)
        assert 100.0 <= h <= 200.0

    def test_datetime_like(self):
        h = hold_seconds_from(self._DT(time.time() - 60.0))
        assert 40.0 <= h <= 120.0

    def test_none_is_zero(self):
        assert hold_seconds_from(None) == 0.0

    def test_future_is_zero(self):
        assert hold_seconds_from(time.time() + 3600.0) == 0.0

    def test_garbage_is_zero(self):
        assert hold_seconds_from("not-a-time") == 0.0

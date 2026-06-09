"""
Tests for the windowed, per-symbol ExecutionMonitor.
Covers: windowing sensitivity, per-symbol isolation, get_typical_spread,
get_size_multiplier, and backward-compatibility with empty state.
"""

from datetime import datetime, timedelta, timezone

import pytest

from brain.execution_monitor import ExecutionMonitor


def _ts(offset_ms: int = 0) -> datetime:
    return datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc) + timedelta(
        milliseconds=offset_ms
    )


def _record(
    mon: ExecutionMonitor,
    symbol: str,
    slippage_pip_equiv: float,
    spread: float = 0.0002,
    latency_ms: float = 50.0,
    pip_size: float = 0.0001,
) -> None:
    """Helper: record a fill whose slippage = slippage_pip_equiv pips."""
    requested = 1.10000
    filled = requested + slippage_pip_equiv * pip_size
    mon.record_execution(
        requested_price=requested,
        filled_price=filled,
        signal_timestamp=_ts(0),
        fill_timestamp=_ts(int(latency_ms)),
        spread=spread,
        pip_size=pip_size,
        symbol=symbol,
    )


# ── 1. Windowing: alert becomes True after a burst, clears after good fills ──


class TestWindowing:
    def test_burst_triggers_alert(self):
        mon = ExecutionMonitor(window=10, slippage_alert_pips=1.2)
        for _ in range(10):
            _record(mon, "EURUSD", slippage_pip_equiv=0.3)
        assert not mon.should_alert(), "low-slip fills should not alert"

        for _ in range(10):
            _record(mon, "EURUSD", slippage_pip_equiv=2.0)
        assert mon.should_alert(), "high-slip burst must trigger alert"

    def test_alert_clears_after_good_fills(self):
        mon = ExecutionMonitor(window=10, slippage_alert_pips=1.2)
        for _ in range(10):
            _record(mon, "EURUSD", slippage_pip_equiv=2.0)
        assert mon.should_alert()

        for _ in range(10):
            _record(mon, "EURUSD", slippage_pip_equiv=0.2)
        assert not mon.should_alert(), "good fills should roll out the burst"

    def test_lifetime_history_does_not_drown_burst(self):
        mon = ExecutionMonitor(window=20, slippage_alert_pips=1.2)
        for _ in range(200):
            _record(mon, "EURUSD", slippage_pip_equiv=0.3)
        stats_before = mon.get_stats(symbol="EURUSD")
        assert stats_before.avg_slippage_pips < 0.5

        for _ in range(20):
            _record(mon, "EURUSD", slippage_pip_equiv=2.0)
        stats_after = mon.get_stats(symbol="EURUSD")
        assert stats_after.avg_slippage_pips > 1.5, (
            "windowed mean must reflect recent burst, not lifetime avg"
        )


# ── 2. Per-symbol isolation ──────────────────────────────────────────────────


class TestPerSymbolIsolation:
    def test_spread_isolation(self):
        mon = ExecutionMonitor(window=50)
        for _ in range(20):
            _record(mon, "XAUUSD", slippage_pip_equiv=0.1, spread=5.0)
        for _ in range(20):
            _record(mon, "EURUSD", slippage_pip_equiv=0.1, spread=0.0002)

        gold_stats = mon.get_stats(symbol="XAUUSD")
        eur_stats = mon.get_stats(symbol="EURUSD")
        assert gold_stats.spread_average > 1.0
        assert eur_stats.spread_average < 0.001
        assert gold_stats.spread_average != eur_stats.spread_average

    def test_slippage_isolation(self):
        mon = ExecutionMonitor(window=50, slippage_alert_pips=1.0)
        for _ in range(20):
            _record(mon, "GBPUSD", slippage_pip_equiv=2.0)
        for _ in range(20):
            _record(mon, "USDJPY", slippage_pip_equiv=0.1)

        gbp_stats = mon.get_stats(symbol="GBPUSD")
        jpy_stats = mon.get_stats(symbol="USDJPY")
        assert gbp_stats.avg_slippage_pips > 1.5
        assert jpy_stats.avg_slippage_pips < 0.5

    def test_should_alert_detects_per_symbol_issue(self):
        mon = ExecutionMonitor(window=50, slippage_alert_pips=1.0)
        for _ in range(20):
            _record(mon, "EURUSD", slippage_pip_equiv=0.1)
        for _ in range(20):
            _record(mon, "GBPUSD", slippage_pip_equiv=2.0)
        assert mon.should_alert(), "alert must fire when ANY symbol is degraded"


# ── 3. get_typical_spread ────────────────────────────────────────────────────


class TestGetTypicalSpread:
    def test_returns_per_symbol_avg(self):
        mon = ExecutionMonitor(window=50)
        for _ in range(10):
            _record(mon, "EURUSD", slippage_pip_equiv=0.1, spread=0.00015)
        for _ in range(10):
            _record(mon, "EURUSD", slippage_pip_equiv=0.1, spread=0.00025)
        avg = mon.get_typical_spread("EURUSD")
        assert avg is not None
        assert abs(avg - 0.0002) < 0.0001

    def test_unknown_symbol_returns_none(self):
        mon = ExecutionMonitor(window=50)
        assert mon.get_typical_spread("NOSYMBOL") is None

    def test_empty_monitor_returns_none(self):
        mon = ExecutionMonitor()
        assert mon.get_typical_spread("EURUSD") is None


# ── 4. get_size_multiplier ───────────────────────────────────────────────────


class TestGetSizeMultiplier:
    def test_excellent_returns_1(self):
        mon = ExecutionMonitor(window=50)
        for _ in range(20):
            _record(mon, "EURUSD", slippage_pip_equiv=0.1, latency_ms=50)
        assert mon.get_size_multiplier("EURUSD") == 1.0

    def test_good_returns_1(self):
        mon = ExecutionMonitor(window=50)
        for _ in range(20):
            _record(mon, "EURUSD", slippage_pip_equiv=0.6, latency_ms=100)
        stats = mon.get_stats(symbol="EURUSD")
        assert stats.execution_quality == "GOOD"
        assert mon.get_size_multiplier("EURUSD") == 1.0

    def test_poor_returns_half(self):
        mon = ExecutionMonitor(window=50)
        for _ in range(20):
            _record(mon, "EURUSD", slippage_pip_equiv=1.2, latency_ms=100)
        stats = mon.get_stats(symbol="EURUSD")
        assert stats.execution_quality == "POOR"
        assert mon.get_size_multiplier("EURUSD") == 0.5

    def test_unacceptable_returns_floor(self):
        mon = ExecutionMonitor(window=50)
        for _ in range(20):
            _record(mon, "EURUSD", slippage_pip_equiv=2.0, latency_ms=800)
        stats = mon.get_stats(symbol="EURUSD")
        assert stats.execution_quality == "UNACCEPTABLE"
        assert mon.get_size_multiplier("EURUSD") == 0.25

    def test_unknown_symbol_returns_1(self):
        mon = ExecutionMonitor(window=50)
        assert mon.get_size_multiplier("NOSYMBOL") == 1.0

    def test_cold_start_returns_1(self):
        mon = ExecutionMonitor()
        assert mon.get_size_multiplier("EURUSD") == 1.0

    def test_never_returns_zero(self):
        mon = ExecutionMonitor(window=10)
        for _ in range(10):
            _record(mon, "EURUSD", slippage_pip_equiv=5.0, latency_ms=2000, spread=0.1)
        mult = mon.get_size_multiplier("EURUSD")
        assert mult > 0, "multiplier must never be zero"
        assert mult == 0.25


# ── 5. Backward-compat: empty monitor ────────────────────────────────────────


class TestBackwardCompat:
    def test_empty_get_stats_returns_zeros(self):
        mon = ExecutionMonitor()
        stats = mon.get_stats()
        assert stats.avg_slippage_pips == 0.0
        assert stats.max_slippage_pips == 0.0
        assert stats.avg_latency_ms == 0.0
        assert stats.spread_current == 0.0
        assert stats.spread_average == 0.0
        assert stats.spread_is_wide is False
        assert stats.requote_count == 0
        assert stats.execution_quality == "EXCELLENT"

    def test_empty_should_alert_false(self):
        mon = ExecutionMonitor()
        assert mon.should_alert() is False

    def test_aggregate_stats_across_symbols(self):
        mon = ExecutionMonitor(window=50)
        for _ in range(10):
            _record(mon, "EURUSD", slippage_pip_equiv=0.2)
        for _ in range(10):
            _record(mon, "GBPUSD", slippage_pip_equiv=0.4)
        agg = mon.get_stats()
        assert 0.2 < agg.avg_slippage_pips < 0.5

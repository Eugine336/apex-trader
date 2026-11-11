"""No-broker integration tests for PlatformManager telemetry wiring."""
import threading
from datetime import datetime, timezone

import pytest
from types import SimpleNamespace

from brain.execution_monitor import ExecutionMonitor
from platforms.platform_manager import PlatformManager


def test_execute_entry_records_broker_outcome_without_changing_return_value():
    monitor = ExecutionMonitor()
    broker_result = SimpleNamespace(
        success=True,
        order_id="test-1",
        fill_price=1.1001,
        requested_price=1.1000,
        slippage_pips=1.0,
        error="",
    )

    class FakeConnector:
        calls = 0

        def place_order(self, *args, **kwargs):
            self.calls += 1
            return broker_result

    connector = FakeConnector()
    manager = object.__new__(PlatformManager)
    manager.execution_monitor = monitor
    manager._broker_write_lock = threading.RLock()
    manager.get_connector = lambda symbol: connector
    manager.get_price = lambda symbol: SimpleNamespace(
        bid=1.0999,
        ask=1.1001,
        time=datetime.now(timezone.utc),
        source_timestamp_verified=True,
        quote_kind="bid_ask",
        symbol=symbol,
    )

    result = manager.execute_entry("EURUSD", "BUY", 0.1, 1.09, 1.12)
    observations = monitor.get_cost_observations("EURUSD")

    assert result is broker_result
    assert connector.calls == 1
    assert observations["market_entry_attempt_count"] == 1
    assert observations["market_entry_success_count"] == 1
    assert observations["market_entry_fill_success_ratio"] == 1.0
    assert observations["observed_submit_response_latency_sample_count"] == 1


def test_execute_entry_monitor_failure_does_not_change_broker_result():
    broker_result = SimpleNamespace(
        success=True, order_id="test-2", fill_price=1.1,
        requested_price=1.1, slippage_pips=0.0, error="",
    )

    class FailingMonitor:
        def record_order_attempt(self, **kwargs):
            raise RuntimeError("telemetry unavailable")

    class FakeConnector:
        def place_order(self, *args, **kwargs):
            return broker_result

    manager = object.__new__(PlatformManager)
    manager.execution_monitor = FailingMonitor()
    manager._broker_write_lock = threading.RLock()
    manager.get_connector = lambda symbol: FakeConnector()
    manager.get_price = lambda symbol: None

    assert manager.execute_entry("EURUSD", "BUY", 0.1, 1.09, 1.12) is broker_result



def test_mt5_entry_and_close_prices_are_paired_without_live_broker_access(monkeypatch):
    from platforms import platform_manager as pm_module
    from platforms.base_connector import CloseResult

    monitor = ExecutionMonitor()
    entry_result = SimpleNamespace(
        success=True, order_id="mt5-ticket-1", symbol="EURUSD", direction="BUY",
        lots=0.1, fill_price=1.1002, requested_price=1.1001,
        slippage_pips=1.0, platform="mt5", error="",
    )
    close_result = CloseResult(
        success=True, order_id="mt5-ticket-1", close_price=1.0998,
        lots_closed=0.1, pnl=-4.0, platform="mt5",
        requested_price=1.0999,
    )

    class FakeConnector:
        def place_order(self, *args, **kwargs):
            return entry_result

        def close_order(self, order_id, lots=None):
            return close_result

    connector = FakeConnector()
    manager = object.__new__(PlatformManager)
    manager.execution_monitor = monitor
    manager._broker_write_lock = threading.RLock()
    manager.get_connector = lambda symbol: connector
    manager._connector_by_platform_str = lambda platform: connector
    manager.get_price = lambda symbol: SimpleNamespace(
        bid=1.1000, ask=1.1002,
        time=datetime.now(timezone.utc),
        source_timestamp_verified=True, quote_kind="bid_ask", symbol=symbol,
    )
    monkeypatch.setattr(
        pm_module, "MT5Connector", FakeConnector,
    )
    monkeypatch.setattr(
        pm_module, "get_instrument",
        lambda symbol: SimpleNamespace(pip_size=0.0001),
    )

    assert manager.execute_entry("EURUSD", "BUY", 0.1, 1.09, 1.12) is entry_result
    assert manager.close_trade("mt5-ticket-1", "mt5", 0.1) is close_result
    observations = monitor.get_cost_observations("EURUSD", min_samples=1)
    assert observations["observed_round_trip_slippage_sample_count"] == 1
    assert observations["observed_round_trip_abs_slippage_avg_pips"] == pytest.approx(2.0)

def test_platform_manager_returns_only_connector_verified_utc_lower_bound():
    manager = object.__new__(PlatformManager)
    manager.get_connector = lambda _symbol: SimpleNamespace(
        get_utc_time_lower_bound=lambda: 1_800_000_000.25,
    )
    assert manager.get_utc_time_lower_bound("XAUUSD") == 1_800_000_000.25

    manager.get_connector = lambda _symbol: SimpleNamespace()
    assert manager.get_utc_time_lower_bound("R_75") is None

    manager.get_connector = lambda _symbol: SimpleNamespace(
        get_utc_time_lower_bound=lambda: float("nan"),
    )
    assert manager.get_utc_time_lower_bound("XAUUSD") is None

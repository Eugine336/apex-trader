"""No-broker integration tests for PlatformManager telemetry wiring."""
import threading
from datetime import datetime, timezone
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

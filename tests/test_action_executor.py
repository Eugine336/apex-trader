"""Tests for execution.action_executor — Phase 6."""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from execution.intents import Intent, IntentType
from execution.action_executor import (
    ActionExecutor,
    ExecutionResult,
    ExecutorConfig,
    ExecutorMetrics,
    _is_transient,
)
from execution.risk_gate import GateConfig


# ── Fake broker ──────────────────────────────────────────────────────

@dataclass
class FakeCloseResult:
    success: bool = True
    error: str = ""
    close_price: float = 1.0850
    lots_closed: float = 0.10
    pnl: float = 12.50


@dataclass
class FakeOpenResult:
    success: bool = True
    error: Optional[str] = None
    order_id: str = "99999"


class FakeBroker:
    """Minimal broker that satisfies BrokerPort."""

    def __init__(self):
        self.modify_calls: list[dict] = []
        self.close_calls: list[dict] = []
        self.modify_return: bool = True
        self.close_return: FakeCloseResult = FakeCloseResult()
        self.raise_on_modify: Optional[Exception] = None
        self.raise_on_close: Optional[Exception] = None
        self.call_count = 0
        self.open_calls: list[dict] = []
        self.open_return: Any = FakeOpenResult()
        self.raise_on_open: Optional[Exception] = None

    def modify_trade(
        self,
        order_id: str,
        platform: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool:
        self.call_count += 1
        if self.raise_on_modify:
            raise self.raise_on_modify
        self.modify_calls.append({
            "order_id": order_id, "platform": platform,
            "new_sl": new_sl, "new_tp": new_tp,
        })
        return self.modify_return

    def close_trade(
        self,
        order_id: str,
        platform: str,
        lots: Optional[float] = None,
    ) -> FakeCloseResult:
        self.call_count += 1
        if self.raise_on_close:
            raise self.raise_on_close
        self.close_calls.append({
            "order_id": order_id, "platform": platform, "lots": lots,
        })
        return self.close_return

    def execute_entry(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        stake_usd: Optional[float] = None,
        idempotency_key: str = "",
    ) -> Any:
        self.call_count += 1
        if self.raise_on_open:
            raise self.raise_on_open
        self.open_calls.append({
            "symbol": symbol, "direction": direction, "lots": lots,
            "sl": sl, "tp": tp,
        })
        return self.open_return


# ── Helpers ──────────────────────────────────────────────────────────

def _positions(**overrides) -> dict[str, dict]:
    base = {
        "12345": {
            "symbol": "EURUSD",
            "direction": "BUY",
            "sl": 1.08000,
            "platform": "mt5",
            "lots": 0.10,
            "remaining_lots": 0.10,
        },
    }
    base["12345"].update(overrides)
    return base


def _close_intent(ticket: str = "12345") -> Intent:
    return Intent.close(symbol="EURUSD", ticket=ticket, source="test", reason="test")


def _open_intent(symbol: str = "V50_1S") -> Intent:
    return Intent.open(
        symbol=symbol, direction="BUY", lots=0.10, sl=1.0, tp=2.0,
        source="test", reason="test",
    )


def _sl_intent(ticket: str = "12345", new_sl: float = 1.08100) -> Intent:
    return Intent.modify_sl(
        symbol="EURUSD", ticket=ticket, new_sl=new_sl,
        source="test", reason="test",
    )


def _tp_intent(ticket: str = "12345", new_tp: float = 1.09000) -> Intent:
    return Intent.modify_tp(
        symbol="EURUSD", ticket=ticket, new_tp=new_tp,
        source="test", reason="test",
    )


def _partial_intent(ticket: str = "12345", fraction: float = 0.5) -> Intent:
    return Intent.partial_close(
        symbol="EURUSD", ticket=ticket, fraction=fraction,
        source="test", reason="test",
    )


def _fast_cfg(**overrides) -> ExecutorConfig:
    defaults = dict(
        max_retries=2,
        retry_base_delay_s=0.01,
        circuit_failure_threshold=5,
        circuit_cooldown_s=0.1,
        gate_config=GateConfig(max_calls_per_second=100),
    )
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


# ── Transient detection ──────────────────────────────────────────────

class TestTransientDetection:
    def test_connection_error_is_transient(self):
        assert _is_transient("Connection refused")

    def test_timeout_is_transient(self):
        assert _is_transient("Request timed out")

    def test_invalid_ticket_is_permanent(self):
        assert not _is_transient("Invalid ticket 12345")

    def test_no_money_is_permanent(self):
        assert not _is_transient("Not enough money")


# ── Close execution ──────────────────────────────────────────────────

class TestCloseExecution:
    def test_successful_close(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_close_intent(), _positions())
        assert result.success
        assert len(broker.close_calls) == 1
        assert broker.close_calls[0]["order_id"] == "12345"
        assert broker.close_calls[0]["platform"] == "mt5"
        assert result.execution_time_ms > 0

    def test_failed_close(self):
        broker = FakeBroker()
        broker.close_return = FakeCloseResult(success=False, error="market closed")
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_close_intent(), _positions())
        assert not result.success
        assert "market closed" in result.error


# ── Modify SL execution ─────────────────────────────────────────────

class TestModifySL:
    def test_successful_modify_sl(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_sl_intent(), _positions())
        assert result.success
        assert len(broker.modify_calls) == 1
        assert broker.modify_calls[0]["new_sl"] == 1.08100

    def test_failed_modify_sl(self):
        broker = FakeBroker()
        broker.modify_return = False
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_sl_intent(), _positions())
        assert not result.success
        assert "returned False" in result.error


# ── Modify TP execution ─────────────────────────────────────────────

class TestModifyTP:
    def test_successful_modify_tp(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_tp_intent(), _positions())
        assert result.success
        assert broker.modify_calls[0]["new_tp"] == 1.09000


# ── Partial close ────────────────────────────────────────────────────

class TestPartialClose:
    def test_successful_partial_close(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_partial_intent(), _positions())
        assert result.success
        assert broker.close_calls[0]["lots"] == 0.05

    def test_zero_lots_rejected(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        positions = _positions(remaining_lots=0.0)
        result = executor.execute(_partial_intent(), positions)
        assert not result.success
        assert "close lots <= 0" in result.error.lower()


# ── Risk gate rejection ─────────────────────────────────────────────

class TestGateRejection:
    def test_missing_position_rejected(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_close_intent("99999"), _positions())
        assert not result.success
        assert result.rejected_by_gate
        assert broker.call_count == 0

    def test_sl_loosening_rejected(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(
            _sl_intent(new_sl=1.07000), _positions(direction="BUY", sl=1.08000),
        )
        assert not result.success
        assert result.rejected_by_gate
        assert broker.call_count == 0

    def test_emergency_drawdown_blocks_modify(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(
            _sl_intent(), _positions(), account_drawdown_pct=25.0,
        )
        assert not result.success
        assert result.rejected_by_gate

    def test_emergency_drawdown_allows_close(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(
            _close_intent(), _positions(), account_drawdown_pct=25.0,
        )
        assert result.success


# ── Retry logic ──────────────────────────────────────────────────────

class TestRetry:
    def test_transient_failure_retried(self):
        broker = FakeBroker()
        call_count = 0

        def flaky_close(order_id, platform, lots=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FakeCloseResult(success=False, error="Connection timeout")
            return FakeCloseResult(success=True)

        broker.close_trade = flaky_close
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_close_intent(), _positions())
        assert result.success
        assert result.retried
        assert call_count == 2

    def test_permanent_failure_not_retried(self):
        broker = FakeBroker()
        broker.close_return = FakeCloseResult(
            success=False, error="Invalid ticket — position not found",
        )
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_close_intent(), _positions())
        assert not result.success
        assert not result.retried
        assert broker.call_count == 1

    def test_connection_error_retried(self):
        broker = FakeBroker()
        call_count = 0

        def raising_close(order_id, platform, lots=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Connection lost")
            return FakeCloseResult(success=True)

        broker.close_trade = raising_close
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_close_intent(), _positions())
        assert result.success
        assert result.retried

    def test_max_retries_exhausted(self):
        broker = FakeBroker()
        broker.close_return = FakeCloseResult(
            success=False, error="Connection timeout",
        )
        executor = ActionExecutor(broker, _fast_cfg(max_retries=1))
        result = executor.execute(_close_intent(), _positions())
        assert not result.success
        assert broker.call_count == 2

    def test_non_connection_error_not_retried(self):
        broker = FakeBroker()
        broker.raise_on_close = ValueError("unexpected internal error")
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_close_intent(), _positions())
        assert not result.success
        assert "ValueError" in result.error
        assert broker.call_count == 1


# ── Circuit breaker ──────────────────────────────────────────────────

class TestCircuitBreaker:
    def test_circuit_opens_after_threshold(self):
        broker = FakeBroker()
        broker.close_return = FakeCloseResult(
            success=False, error="Invalid ticket",
        )
        cfg = _fast_cfg(
            max_retries=0,
            circuit_failure_threshold=3,
            circuit_cooldown_s=10.0,
        )
        executor = ActionExecutor(broker, cfg)
        for _ in range(3):
            executor.execute(_close_intent(), _positions())

        result = executor.execute(_close_intent(), _positions())
        assert not result.success
        assert "circuit open" in result.error.lower()
        metrics = executor.get_metrics()
        assert metrics.intents_circuit_open >= 1

    def test_close_failures_do_not_block_open(self):
        """A tripped CLOSE/manage breaker must NOT block new OPEN entries.

        Regression for the production incident where weekend close failures
        tripped the shared breaker and starved 24/7 synthetic entries.  OPEN
        and CLOSE now use independent breakers. A genuine permanent failure
        (invalid ticket) is used to trip the manage breaker — market-closed is
        deliberately excluded from the breaker (see
        test_market_closed_does_not_trip_breaker).
        """
        broker = FakeBroker()
        broker.close_return = FakeCloseResult(
            success=False, error="Invalid ticket — position not found",
        )
        cfg = _fast_cfg(
            max_retries=0,
            circuit_failure_threshold=3,
            circuit_cooldown_s=10.0,
        )
        executor = ActionExecutor(broker, cfg)

        # Trip the CLOSE/manage breaker.
        for _ in range(3):
            executor.execute(_close_intent(), _positions())
        blocked = executor.execute(_close_intent(), _positions())
        assert "circuit open" in (blocked.error or "").lower()

        # OPEN path must remain available — its breaker is independent.
        opened = executor.execute(_open_intent(), _positions())
        assert opened.success, (
            "OPEN was blocked by the CLOSE breaker — breakers are not isolated"
        )
        assert len(broker.open_calls) == 1

    def test_market_closed_does_not_trip_breaker(self):
        """Market-closed close failures must never open the manage breaker.

        A closed weekend/holiday market is an expected condition, not a broker
        fault. Repeated market-closed closes must not count toward the failure
        budget (otherwise the manage breaker opens and blocks legitimate
        closes/modifies on 24/7 instruments), and must not be retried.
        """
        broker = FakeBroker()
        broker.close_return = FakeCloseResult(
            success=False, error="MARKET_CLOSED: Market closed",
        )
        cfg = _fast_cfg(
            max_retries=2,
            circuit_failure_threshold=3,
            circuit_cooldown_s=10.0,
        )
        executor = ActionExecutor(broker, cfg)

        # Far more attempts than the failure threshold — the breaker must stay
        # closed because market-closed does not count as a failure.
        for _ in range(10):
            r = executor.execute(_close_intent(), _positions())
            assert not r.success
            assert "market" in (r.error or "").lower()
            # Never retried (no point — the market stays closed).
            assert r.retried is False
        still_open = executor.execute(_close_intent(), _positions())
        assert "circuit open" not in (still_open.error or "").lower()

    def test_open_failures_do_not_block_close(self):
        """Symmetric: a tripped OPEN breaker must not block exits/closes."""
        broker = FakeBroker()
        broker.open_return = FakeOpenResult(success=False, error="open rejected")
        cfg = _fast_cfg(
            max_retries=0,
            circuit_failure_threshold=3,
            circuit_cooldown_s=10.0,
        )
        executor = ActionExecutor(broker, cfg)

        for _ in range(3):
            executor.execute(_open_intent(), _positions())
        blocked = executor.execute(_open_intent(), _positions())
        assert "circuit open" in (blocked.error or "").lower()

        # CLOSE path must remain available so risk exits are never starved.
        closed = executor.execute(_close_intent(), _positions())
        assert closed.success, (
            "CLOSE was blocked by the OPEN breaker — breakers are not isolated"
        )


# ── Batch execution ──────────────────────────────────────────────────

class TestBatchExecution:
    def test_batch_executes_in_priority_order(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        intents = [
            _sl_intent(),
            _close_intent(),
            _tp_intent(),
        ]
        results = executor.execute_batch(intents, _positions())
        assert len(results) == 3
        assert results[0].intent.intent_type == IntentType.CLOSE
        assert results[1].intent.intent_type == IntentType.PARTIAL_CLOSE or \
               results[1].intent.intent_type == IntentType.MODIFY_SL

    def test_batch_removes_closed_positions(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        positions = _positions()
        intents = [
            _close_intent(),
            _sl_intent(),
        ]
        results = executor.execute_batch(intents, positions)
        close_result = results[0]
        sl_result = results[1]
        assert close_result.success
        assert not sl_result.success
        assert sl_result.rejected_by_gate


# ── Metrics ──────────────────────────────────────────────────────────

class TestMetrics:
    def test_initial_metrics(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        m = executor.get_metrics()
        assert m.intents_received == 0
        assert m.intents_executed == 0
        assert m.intents_rejected == 0
        assert m.intents_failed == 0

    def test_successful_execution_metrics(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        executor.execute(_close_intent(), _positions())
        m = executor.get_metrics()
        assert m.intents_received == 1
        assert m.intents_executed == 1
        assert m.total_execution_time_ms > 0

    def test_rejected_metrics(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        executor.execute(_close_intent("99999"), _positions())
        m = executor.get_metrics()
        assert m.intents_received == 1
        assert m.intents_rejected == 1
        assert m.intents_executed == 0

    def test_failed_metrics(self):
        broker = FakeBroker()
        broker.close_return = FakeCloseResult(
            success=False, error="Invalid ticket",
        )
        executor = ActionExecutor(broker, _fast_cfg(max_retries=0))
        executor.execute(_close_intent(), _positions())
        m = executor.get_metrics()
        assert m.intents_failed == 1

    def test_retry_metrics(self):
        broker = FakeBroker()
        call_count = 0

        def flaky(order_id, platform, lots=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FakeCloseResult(success=False, error="timeout")
            return FakeCloseResult(success=True)

        broker.close_trade = flaky
        executor = ActionExecutor(broker, _fast_cfg())
        executor.execute(_close_intent(), _positions())
        m = executor.get_metrics()
        assert m.total_retries >= 1
        assert m.intents_executed == 1

    def test_avg_execution_time(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        executor.execute(_close_intent(), _positions())
        executor.execute(_sl_intent(), _positions())
        m = executor.get_metrics()
        assert m.avg_execution_time_ms > 0

    def test_avg_execution_time_zero_when_empty(self):
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        m = executor.get_metrics()
        assert m.avg_execution_time_ms == 0.0


# ── Serialization ────────────────────────────────────────────────────

class TestSerialization:
    def test_concurrent_executions_serialized(self):
        broker = FakeBroker()
        execution_order: list[str] = []
        lock = threading.Lock()

        _original_close = broker.close_trade

        def tracking_close(order_id, platform, lots=None):
            with lock:
                execution_order.append(f"start_{order_id}")
            time.sleep(0.02)
            result = FakeCloseResult(success=True)
            with lock:
                execution_order.append(f"end_{order_id}")
            return result

        broker.close_trade = tracking_close
        executor = ActionExecutor(broker, _fast_cfg())

        positions = {
            "A": {"symbol": "EURUSD", "direction": "BUY", "sl": 1.08, "platform": "mt5"},
            "B": {"symbol": "GBPUSD", "direction": "SELL", "sl": 1.26, "platform": "mt5"},
        }
        intent_a = Intent.close(symbol="EURUSD", ticket="A", source="t", reason="t")
        intent_b = Intent.close(symbol="GBPUSD", ticket="B", source="t", reason="t")

        threads = [
            threading.Thread(target=executor.execute, args=(intent_a, positions)),
            threading.Thread(target=executor.execute, args=(intent_b, positions)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(execution_order) == 4
        assert execution_order[0].startswith("start_")
        assert execution_order[1].startswith("end_")
        assert execution_order[2].startswith("start_")
        assert execution_order[3].startswith("end_")

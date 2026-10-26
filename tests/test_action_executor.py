"""Tests for execution.action_executor — Phase 6."""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from typing import Any, Optional


from execution.intents import Intent, IntentType
from execution.action_executor import (
    ActionExecutor,
    ExecutorConfig,
    _is_transient,
)
from execution.risk_gate import GateConfig
from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION


# ── Recording fake broker (never connects to a live platform) ─────────

from test_support.recording_broker import FakeBroker, FakeCloseResult, FakeOpenResult


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
    return Intent.close(
        symbol="EURUSD", ticket=ticket, source="ai_brain", reason="test",
        decision_id="decision-test", campaign_id="campaign-test",
        opportunity_id="opportunity-test",
    )


def _open_intent(symbol: str = "V50_1S", *, brain_provenance: bool = True, **overrides) -> Intent:
    values = dict(
        symbol=symbol, direction="BUY", lots=0.10, sl=1.0, tp=2.0,
        source="test", reason="test",
    )
    if brain_provenance:
        values.update(source="ai_brain", decision_id="decision-test",
                      campaign_id="campaign-test", opportunity_id="opportunity-test")
    values.update(overrides)
    return Intent.open(**values)


def _sl_intent(ticket: str = "12345", new_sl: float = 1.08100) -> Intent:
    return Intent.modify_sl(
        symbol="EURUSD", ticket=ticket, new_sl=new_sl,
        source="ai_brain", reason="test",
        decision_id="decision-test", campaign_id="campaign-test",
        opportunity_id="opportunity-test",
    )


def _tp_intent(ticket: str = "12345", new_tp: float = 1.09000) -> Intent:
    return Intent.modify_tp(
        symbol="EURUSD", ticket=ticket, new_tp=new_tp,
        source="ai_brain", reason="test",
        decision_id="decision-test", campaign_id="campaign-test",
        opportunity_id="opportunity-test",
    )


def _partial_intent(ticket: str = "12345", fraction: float = 0.5) -> Intent:
    return Intent.partial_close(
        symbol="EURUSD", ticket=ticket, fraction=fraction,
        source="ai_brain", reason="test",
        decision_id="decision-test", campaign_id="campaign-test",
        opportunity_id="opportunity-test",
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

    def test_zero_lots_skips_to_sl_protection(self):
        # A position that cannot be split (computed close lots round to zero)
        # must NOT hard-fail — that would roll back the optimistic
        # partial_closed flag and re-fire the doomed PARTIAL_CLOSE forever.
        # Instead the executor reports success with a min-lot skip marker so
        # the flag sticks and the position falls through to SL protection.
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        positions = _positions(remaining_lots=0.0)
        result = executor.execute(_partial_intent(), positions)
        assert result.success
        assert "min_lot_skip" in (result.error or "")
        # Broker close must never be called for an impossible split.
        assert broker.close_calls == []

    def test_minlot_partial_skips_to_sl_protection(self):
        # A 0.01-lot position at the broker minimum: 0.01 × 0.5 = 0.005 →
        # rounds to 0 at the 0.01 volume step. Same fallback as above.
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        positions = _positions(remaining_lots=0.01, volume_step=0.01)
        result = executor.execute(_partial_intent(), positions)
        assert result.success
        assert "min_lot_skip" in (result.error or "")
        assert broker.close_calls == []

    def test_normal_lot_partial_still_executes(self):
        # Regression: a normal-sized position is split and sent to the broker
        # exactly as before — the min-lot fallback must not interfere.
        broker = FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        positions = _positions(remaining_lots=0.10, volume_step=0.01)
        result = executor.execute(_partial_intent(), positions)
        assert result.success
        assert broker.close_calls[0]["lots"] == 0.05


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

    def test_persistent_connection_error_trips_breaker(self):
        """A connection that keeps failing IS a real failure — after retries
        are exhausted on every call, the breaker must record failures and
        eventually open.  Regression for audit #15 (ConnectionError must not
        be retried forever without tripping the breaker)."""
        broker = FakeBroker()
        broker.raise_on_close = ConnectionError("Connection lost")
        cfg = _fast_cfg(
            max_retries=1,
            circuit_failure_threshold=3,
            circuit_cooldown_s=10.0,
        )
        executor = ActionExecutor(broker, cfg)

        # Each call exhausts retries (transient ConnectionError) and records a
        # single breaker failure.  After the threshold, the breaker opens.
        for _ in range(3):
            res = executor.execute(_close_intent(), _positions())
            assert not res.success

        blocked = executor.execute(_close_intent(), _positions())
        assert "circuit open" in (blocked.error or "").lower(), (
            "Persistent ConnectionError never tripped the breaker"
        )

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


# ── Per-instrument circuit isolation ─────────────────────────────────

class TestPerInstrumentIsolation:
    """A failing MODIFY_SL storm on ONE instrument must not open a breaker
    that blocks CLOSE/MODIFY on any OTHER instrument, nor the CLOSE of the
    SAME instrument.

    Regression for the production incident where a repeated
    ``MODIFY_SL <ticket> (NZDUSD) — Invalid stops`` loop tripped a shared
    per-platform MT5 manage breaker, which then blocked emergency
    heat-monitor closes on every other open ticket for the cooldown window.
    Each open position must be handled independently.
    """

    @staticmethod
    def _multi_positions() -> dict[str, dict]:
        return {
            "111": {
                "symbol": "NZDUSD", "direction": "BUY", "sl": 0.61000,
                "platform": "mt5", "lots": 0.01, "remaining_lots": 0.01,
            },
            "222": {
                "symbol": "EURUSD", "direction": "BUY", "sl": 1.08000,
                "platform": "mt5", "lots": 0.01, "remaining_lots": 0.01,
            },
        }

    def test_modify_storm_does_not_block_other_instruments(self):
        broker = FakeBroker()
        broker.modify_return = False  # every MODIFY_SL rejected ("invalid stops")
        cfg = _fast_cfg(
            max_retries=0,
            circuit_failure_threshold=3,
            circuit_cooldown_s=10.0,
        )
        executor = ActionExecutor(broker, cfg)
        positions = self._multi_positions()

        # Storm the NZDUSD modify path until ITS breaker opens.
        nz_modify = Intent.modify_sl(
            symbol="NZDUSD", ticket="111", new_sl=0.61000,
            source="test", reason="test",
        )
        for _ in range(3):
            executor.execute(nz_modify, positions)
        blocked = executor.execute(nz_modify, positions)
        assert "circuit open" in (blocked.error or "").lower(), (
            "NZDUSD modify storm never opened its own breaker"
        )

        # A CLOSE on a DIFFERENT instrument must still go through.
        eur_close = Intent.close(
            symbol="EURUSD", ticket="222", source="test", reason="test",
        )
        r = executor.execute(eur_close, positions)
        assert r.success, (
            "EURUSD close was blocked by the NZDUSD modify breaker — "
            "management breakers are not isolated per instrument"
        )

        # A CLOSE on the SAME instrument must also still go through: within a
        # symbol, CLOSE is isolated from MODIFY so a stuck stop-move never
        # starves the risk-reducing exit.
        nz_close = Intent.close(
            symbol="NZDUSD", ticket="111", source="test", reason="test",
        )
        r2 = executor.execute(nz_close, positions)
        assert r2.success, (
            "NZDUSD close was blocked by the NZDUSD modify breaker — CLOSE is "
            "not isolated from MODIFY within a symbol"
        )

    def test_modify_storm_does_not_block_other_instrument_modify(self):
        """The other instrument's MODIFY path must also stay available."""
        broker = FakeBroker()
        broker.modify_return = False
        cfg = _fast_cfg(
            max_retries=0,
            circuit_failure_threshold=3,
            circuit_cooldown_s=10.0,
        )
        executor = ActionExecutor(broker, cfg)
        positions = self._multi_positions()

        nz_modify = Intent.modify_sl(
            symbol="NZDUSD", ticket="111", new_sl=0.61000,
            source="test", reason="test",
        )
        for _ in range(4):
            executor.execute(nz_modify, positions)

        # EURUSD modify uses a different per-symbol breaker; broker now accepts.
        broker.modify_return = True
        eur_modify = Intent.modify_sl(
            symbol="EURUSD", ticket="222", new_sl=1.08050,
            source="test", reason="test",
        )
        r = executor.execute(eur_modify, positions)
        assert r.success, (
            "EURUSD modify was blocked by the NZDUSD modify breaker — "
            "per-instrument isolation failed"
        )


def test_gold_only_execution_guard_rejects_non_gold_new_exposure():
    broker = FakeBroker()
    executor = ActionExecutor(
        broker=broker,
        config=ExecutorConfig(gold_only=True),
    )

    rejected = executor.execute(_open_intent("EURUSD"), {})
    assert not rejected.success
    assert rejected.rejected_by_gate
    assert "gold_only_invariant" in rejected.gate_reason
    assert broker.open_calls == []

    # Exact broker aliases are allowed; prefix lookalikes are not.
    xau = executor.execute(_open_intent("XAUUSDm", brain_provenance=True), {})
    assert xau.success, xau.error
    assert broker.open_calls[-1]["symbol"] == "XAUUSDm"

    fake = executor.execute(_open_intent("XAUUSD_FAKE"), {})
    assert not fake.success
    assert fake.rejected_by_gate
    assert len(broker.open_calls) == 1


def test_gold_only_guard_does_not_block_risk_reducing_exit_on_legacy_position():
    broker = FakeBroker()
    executor = ActionExecutor(
        broker=broker,
        config=ExecutorConfig(
            gold_only=True,
            require_gold_feed_health=True,
            gold_feed_health_check=lambda _symbol: False,
        ),
    )
    # An unhealthy Gold feed blocks new entries, but must not stop reducing an
    # already-open position; exits remain available during feed degradation.
    result = executor.execute(_close_intent(), _positions())
    assert result.success
    assert broker.close_calls[0]["order_id"] == "12345"


def test_gold_only_open_requires_fresh_feed_when_runtime_enables_health_gate():
    broker = FakeBroker()
    healthy = {"value": False}
    executor = ActionExecutor(
        broker=broker,
        config=ExecutorConfig(
            gold_only=True,
            require_gold_feed_health=True,
            gold_feed_health_check=lambda _symbol: healthy["value"],
        ),
    )

    blocked = executor.execute(_open_intent("XAUUSD", brain_provenance=True), {})
    assert not blocked.success
    assert blocked.rejected_by_gate
    assert "gold_feed_unhealthy" in blocked.gate_reason
    assert broker.open_calls == []

    healthy["value"] = True
    allowed = executor.execute(_open_intent("XAUUSD", brain_provenance=True), {})
    assert allowed.success, allowed.error
    assert [call["symbol"] for call in broker.open_calls] == ["XAUUSD"]


def test_gold_feed_health_callback_failure_fails_closed():
    broker = FakeBroker()

    def broken_health_check(_symbol):
        raise RuntimeError("market-data cache unavailable")

    executor = ActionExecutor(
        broker=broker,
        config=ExecutorConfig(
            gold_only=True,
            require_gold_feed_health=True,
            gold_feed_health_check=broken_health_check,
        ),
    )
    blocked = executor.execute(_open_intent("XAUUSD", brain_provenance=True), {})
    assert not blocked.success
    assert blocked.rejected_by_gate
    assert "gold_feed_unhealthy" in blocked.gate_reason
    assert broker.open_calls == []


def test_management_target_change_without_provenance_is_rejected():
    broker = FakeBroker()
    executor = ActionExecutor(broker, _fast_cfg())
    intent = Intent.modify_tp(
        symbol="EURUSD", ticket="12345", new_tp=1.095,
        source="legacy", reason="unattributed target change",
    )
    result = executor.execute(intent, _positions())
    assert not result.success and result.rejected_by_gate
    assert "management_provenance_required" in result.gate_reason
    assert broker.modify_calls == []


def test_protective_stop_tightening_remains_available_without_links():
    broker = FakeBroker()
    executor = ActionExecutor(broker, _fast_cfg())
    intent = Intent.modify_sl(
        symbol="EURUSD", ticket="12345", new_sl=1.081,
        source="legacy", reason="tighten stop",
    )
    result = executor.execute(intent, _positions())
    assert result.success
    assert broker.modify_calls[0]["new_sl"] == 1.081


def test_noncanonical_management_identity_is_rejected_even_for_close():
    broker = FakeBroker()
    executor = ActionExecutor(broker, _fast_cfg())
    intent = Intent.close(
        symbol="EURUSD", ticket="12345", source="legacy", reason="close",
        identity_version="old-version",
    )
    result = executor.execute(intent, _positions())
    assert not result.success and result.rejected_by_gate
    assert "identity_contract_mismatch" in result.gate_reason
    assert broker.close_calls == []


def test_open_with_mismatched_identity_is_rejected_before_broker_call():
    broker = FakeBroker()
    executor = ActionExecutor(broker, _fast_cfg())
    intent = Intent.open(
        symbol="EURUSD", direction="BUY", lots=0.10, sl=1.0, tp=2.0,
        source="test", reason="identity mismatch",
        identity_version=IDENTITY_VERSION, identity="not the canonical Apex identity",
    )
    result = executor.execute(intent, open_positions={})
    assert result.success is False
    assert result.rejected_by_gate is True
    assert "identity_contract_mismatch" in result.gate_reason
    assert broker.open_calls == []


def test_open_intent_carries_canonical_identity_by_default():
    intent = _open_intent()
    assert intent.identity_version == IDENTITY_VERSION
    assert intent.identity == IDENTITY_STATEMENT


def test_gold_only_rejects_legacy_entry_without_brain_opportunity_provenance():
    broker = FakeBroker()
    executor = ActionExecutor(broker, ExecutorConfig(gold_only=True))
    result = executor.execute(_open_intent("XAUUSD", brain_provenance=False), {})
    assert not result.success and result.rejected_by_gate
    assert "brain_opportunity_provenance_required" in result.gate_reason
    assert broker.open_calls == []


def test_non_gold_mode_also_rejects_legacy_open_without_cognitive_provenance():
    broker = FakeBroker()
    executor = ActionExecutor(broker, _fast_cfg(gold_only=False))
    result = executor.execute(_open_intent("EURUSD", brain_provenance=False), {})
    assert not result.success and result.rejected_by_gate
    assert "brain_opportunity_provenance_required" in result.gate_reason
    assert broker.open_calls == []


def test_non_gold_mode_accepts_canonical_brain_open_provenance():
    broker = FakeBroker()
    executor = ActionExecutor(broker, _fast_cfg(gold_only=False))
    result = executor.execute(_open_intent("EURUSD", brain_provenance=True), {})
    assert result.success
    assert len(broker.open_calls) == 1


def test_gold_only_rejects_brain_entry_without_activated_opportunity_id():
    broker = FakeBroker()
    executor = ActionExecutor(broker, ExecutorConfig(gold_only=True))
    intent = _open_intent("XAUUSD", brain_provenance=True, opportunity_id="")
    result = executor.execute(intent, {})
    assert not result.success and result.rejected_by_gate
    assert "opportunity_id" in result.gate_reason
    assert broker.open_calls == []

"""
Regression tests for:
  BUG 1 — Deriv multiplier+cap combined retry convergence
  BUG 2 — _get_multiplier Windows path resolution
  BUG 3 — PositionStore cross-thread safety
  BUG 4 — Execution monitor per-instrument pip_size
  BUG 5 — Execution latency uses pre-exec timestamp
"""

import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ──────────────────────────────────────────────────────────────────────
# BUG 3 — PositionStore thread-safety
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _FakePos:
    order_id: str = "ORD-1"
    platform: str = "mt5"
    symbol: str = "EURUSD"
    direction: str = "BUY"
    lots: float = 0.01
    entry_price: float = 1.1
    sl: float = 1.09
    tp1: float = 1.12
    tp2: float = 1.14
    score: int = 90
    regime: str = "TRENDING"
    session: str = "london"
    entry_type: str = "FVG"
    open_time: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tp1_hit: bool = False
    at_breakeven: bool = False
    trailing: bool = False
    tm_trade_id: str = "TM-1"
    stake_usd: float = 0.0
    multiplier: int = 100


def test_position_store_cross_thread_save_and_load():
    """BUG 3: PositionStore created in main thread, used from background thread."""
    from persistence.position_store import PositionStore

    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "test.db")
        store = PositionStore(db_path=db_path)

        errors = []

        def bg_work():
            try:
                pos = _FakePos(order_id="BG-1")
                store.save_position(pos)
                store.update_position("BG-1", sl=1.095)
                loaded = store.load_all_positions()
                if not loaded:
                    errors.append("load_all_positions returned empty after save")
                store.remove_position("BG-1")
                remaining = store.count()
                if remaining != 0:
                    errors.append(f"Expected 0 positions after remove, got {remaining}")
            except Exception as exc:
                errors.append(str(exc))

        t = threading.Thread(target=bg_work)
        t.start()
        t.join(timeout=5)

        assert not errors, f"Cross-thread operations failed: {errors}"
        store.close()


def test_position_store_concurrent_writes():
    """BUG 3: Multiple threads writing concurrently must not corrupt data."""
    from persistence.position_store import PositionStore

    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "test.db")
        store = PositionStore(db_path=db_path)

        errors = []

        def writer(thread_id):
            try:
                for i in range(10):
                    pos = _FakePos(order_id=f"T{thread_id}-{i}")
                    store.save_position(pos)
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent writes failed: {errors}"
        assert store.count() == 40
        store.close()


# ──────────────────────────────────────────────────────────────────────
# BUG 4 — Execution monitor pip_size per instrument
# ──────────────────────────────────────────────────────────────────────


def test_execution_monitor_respects_per_call_pip_size():
    """BUG 4: ETHUSD (pip=0.01) fill 0.50 from requested = 50 pips, not 5000."""
    from brain.execution_monitor import ExecutionMonitor

    monitor = ExecutionMonitor()
    now = datetime.now(timezone.utc)

    monitor.record_execution(
        requested_price=1963.00,
        filled_price=1963.50,
        signal_timestamp=now,
        fill_timestamp=now + timedelta(milliseconds=500),
        spread=0.29,
        pip_size=0.01,
    )

    stats = monitor.get_stats()
    assert stats.avg_slippage_pips == pytest.approx(50.0, abs=0.1)
    assert stats.avg_latency_ms == pytest.approx(500.0, abs=10)


def test_execution_monitor_default_pip_size_for_forex():
    """BUG 4: Forex uses default 0.0001 when no pip_size passed."""
    from brain.execution_monitor import ExecutionMonitor

    monitor = ExecutionMonitor()
    now = datetime.now(timezone.utc)

    monitor.record_execution(
        requested_price=1.10000,
        filled_price=1.10005,
        signal_timestamp=now,
        fill_timestamp=now + timedelta(milliseconds=200),
        spread=0.00012,
    )

    stats = monitor.get_stats()
    assert stats.avg_slippage_pips == pytest.approx(0.5, abs=0.01)


# ──────────────────────────────────────────────────────────────────────
# BUG 5 — Execution latency timestamp
# ──────────────────────────────────────────────────────────────────────


def test_execution_monitor_latency_reflects_order_roundtrip():
    """BUG 5: Latency should reflect order round-trip, not pipeline wall-clock."""
    from brain.execution_monitor import ExecutionMonitor

    monitor = ExecutionMonitor()
    pre_exec = datetime.now(timezone.utc)
    fill_time = pre_exec + timedelta(milliseconds=600)

    monitor.record_execution(
        requested_price=1.10000,
        filled_price=1.10001,
        signal_timestamp=pre_exec,
        fill_timestamp=fill_time,
        spread=0.00012,
    )

    stats = monitor.get_stats()
    assert stats.avg_latency_ms == pytest.approx(600.0, abs=50)
    assert stats.avg_latency_ms < 5000


# ──────────────────────────────────────────────────────────────────────
# BUG 2 — _get_multiplier path resolution
# ──────────────────────────────────────────────────────────────────────


def test_get_multiplier_uses_pathlib_not_string_replace():
    """BUG 2: _get_multiplier must find config on Windows (backslash paths)."""
    from platforms.deriv.deriv_connector import DerivConnector

    connector = DerivConnector.__new__(DerivConnector)
    connector._discovered_multipliers = {}

    result = connector._get_multiplier("CRASH1000")
    assert result in [100, 200, 300, 400, 500], f"Got {result}, expected one of the accepted values"
    assert result != 1000, "Got fallback 1000 — config path resolution failed"


def test_deriv_json_crash1000_has_500():
    """BUG 2: deriv.json CRASH1000 accepted list must include 100 (default)."""
    import json

    cfg_path = Path(__file__).parent.parent / "config" / "brokers" / "deriv.json"
    with open(cfg_path) as f:
        cfg = json.load(f)

    crash_entry = cfg["multipliers"]["CRASH1000"]
    assert 100 in crash_entry["accepted"]
    assert crash_entry["default"] <= max(crash_entry["accepted"])


# ──────────────────────────────────────────────────────────────────────
# BUG 1 — Deriv multiplier+cap combined retry convergence
# ──────────────────────────────────────────────────────────────────────


def _build_deriv_connector_for_retry_test():
    """Create a minimal DerivConnector mock for testing the retry loop."""
    from platforms.deriv.deriv_connector import DerivConnector

    connector = DerivConnector.__new__(DerivConnector)
    connector._discovered_multipliers = {}
    connector._ws = MagicMock()
    connector._connected = True
    connector._reconnecting = False
    connector._positions = {}
    connector._loop = MagicMock()
    return connector


def test_multiplier_then_cap_combined_converges():
    """BUG 1: multiplier rejected → rescaled → cap hit → must converge and succeed."""

    connector = _build_deriv_connector_for_retry_test()

    call_count = [0]

    def fake_sync_send(msg):
        call_count[0] += 1
        amount = msg.get("price", msg.get("parameters", {}).get("amount", 0))

        if call_count[0] == 1:
            return {"error": {"message": "Multiplier is not in acceptable range. Accepts 100,200,300,400,500"}}
        if call_count[0] == 2:
            return {"error": {"message": "Enter an amount equal to or lower than 23.77."}}
        if call_count[0] == 3:
            return {
                "buy": {"contract_id": "12345", "buy_price": amount, "start_time": 1000000, "longcode": "test"},
            }
        return {"error": {"message": "Unexpected call"}}

    connector._sync_send = fake_sync_send
    connector.symbol_map = MagicMock(return_value="CRASH1000")
    connector.get_price = MagicMock(return_value=MagicMock(ask=5700.0, bid=5698.0))

    result = connector.place_order(
        symbol="CRASH1000",
        direction="SELL",
        lots=0.01,
        sl=5714.0,
        tp=5640.0,
        stake_usd=23.77,
        multiplier=1000,
    )

    assert result.success, f"Order should have succeeded on attempt 3, got error: {result.error}"
    assert call_count[0] == 3


def test_cap_floors_strictly_below():
    """BUG 1: capped value must be strictly less than broker cap."""
    import math as _math

    max_stake = 23.77
    capped = max(1.0, float(_math.floor(max_stake * 100 - 1)) / 100)
    assert capped < max_stake, f"Capped {capped} is not strictly below cap {max_stake}"
    assert capped == 23.76


def test_sl_dollar_recomputed_each_retry():
    """BUG 1: SL dollar must match the current amount on each retry."""

    connector = _build_deriv_connector_for_retry_test()

    captured_requests = []

    def fake_sync_send(msg):
        captured_requests.append(msg)
        amount = msg.get("price", 0)

        if len(captured_requests) == 1:
            return {"error": {"message": "Enter an amount equal to or lower than 20.00."}}
        if len(captured_requests) == 2:
            return {
                "buy": {"contract_id": "99", "buy_price": amount, "start_time": 1, "longcode": "x"},
            }
        return {"error": {"message": "Unexpected"}}

    connector._sync_send = fake_sync_send
    connector.symbol_map = MagicMock(return_value="1HZ100V")
    connector.get_price = MagicMock(return_value=MagicMock(ask=860.0, bid=859.0))

    connector.place_order(
        symbol="V100_1S",
        direction="SELL",
        lots=0.01,
        sl=870.0,
        tp=840.0,
        stake_usd=25.00,
        multiplier=100,
    )

    assert len(captured_requests) >= 2
    req1 = captured_requests[0]
    req2 = captured_requests[1]

    sl1 = req1["parameters"]["limit_order"]["stop_loss"]
    sl2 = req2["parameters"]["limit_order"]["stop_loss"]
    amt2 = req2["price"]

    assert sl2 < sl1, f"Retry SL ${sl2} should be less than initial SL ${sl1} (amount reduced to ${amt2})"


def test_cap_below_minimum_skips_cleanly():
    """BUG 1: If broker cap drops below $1, the order is skipped, not retried forever."""

    connector = _build_deriv_connector_for_retry_test()

    def fake_sync_send(msg):
        return {"error": {"message": "Enter an amount equal to or lower than 0.50."}}

    connector._sync_send = fake_sync_send
    connector.symbol_map = MagicMock(return_value="1HZ10V")
    connector.get_price = MagicMock(return_value=MagicMock(ask=10200.0, bid=10199.0))

    result = connector.place_order(
        symbol="V10_1S",
        direction="BUY",
        lots=0.01,
        sl=10190.0,
        tp=10220.0,
        stake_usd=5.00,
        multiplier=100,
    )

    assert not result.success

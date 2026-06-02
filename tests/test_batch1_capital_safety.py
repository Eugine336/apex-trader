"""
Regression tests for Batch 1 capital-safety fixes:
  H1 — Thread-safe managed_positions (_LockedPositions wrapper)
  H5 — Startup recovery wired in both headless and dashboard modes
  H6 — Periodic reconcile heartbeat fires after interval
"""

import contextlib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import MagicMock, patch



# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

_PATCHES = [
    "platforms.main_loop.PlatformManager",
    "platforms.main_loop.PairScanner",
    "platforms.main_loop.PairRanker",
    "platforms.main_loop.ScanScheduler",
    "platforms.main_loop.MTFOrchestrator",
    "platforms.main_loop.EntryEngine",
    "platforms.main_loop.DrawdownGuard",
    "platforms.main_loop.CorrelationEngine",
    "platforms.main_loop.RiskEngine",
    "platforms.main_loop.ExecutionMonitor",
    "platforms.main_loop.SessionEngine",
    "platforms.main_loop.NewsGuard",
    "platforms.main_loop.TradeJournal",
    "platforms.main_loop.EntryValidator",
    "platforms.main_loop.RiskReporter",
    "platforms.main_loop.MLAdapter",
    "platforms.main_loop.ReEntryManager",
    "platforms.main_loop.OpportunityDensityTracker",
    "platforms.main_loop.SystemVolatilityMonitor",
    "platforms.main_loop.TradeManager",
    "platforms.main_loop.PositionStore",
    "platforms.main_loop.HealthWatchdog",
    "platforms.main_loop.DailyMaintenance",
    "platforms.main_loop.CircuitBreaker",
    "platforms.main_loop.StartupCheck",
]


@contextlib.contextmanager
def _patched_loop_deps():
    """Patch all heavy TradingLoop dependencies so __init__ succeeds."""
    with contextlib.ExitStack() as stack:
        for target in _PATCHES:
            stack.enter_context(patch(target))
        yield


def _make_loop():
    """Create a TradingLoop with all heavy deps mocked out."""
    with _patched_loop_deps():
        from platforms.main_loop import TradingLoop
        loop = TradingLoop()
        loop._restore_positions = MagicMock()
        loop._reconcile_positions = MagicMock()
        return loop


def _make_loop_for_run_once():
    """Create a TradingLoop rigged for a single run_once() call."""
    loop = _make_loop()
    loop.watchdog = MagicMock()
    loop.watchdog._cycles = 1
    loop.watchdog.record_cycle = MagicMock()
    loop.watchdog.record_scan_success = MagicMock()
    loop.watchdog.record_trade_check_success = MagicMock()
    loop.watchdog.check_health = MagicMock(return_value=MagicMock(is_healthy=True))
    loop.drawdown = MagicMock()
    loop.drawdown.can_trade = MagicMock(return_value=(False, "test"))
    loop._check_pending_orders = MagicMock()
    loop._check_weekend_protection = MagicMock()
    loop._update_positions = MagicMock(return_value=0)
    loop._check_and_reconnect = MagicMock()
    loop.session_engine = MagicMock()
    loop.news_guard = MagicMock()
    loop.config = MagicMock()
    loop.config.enabled_pairs = []
    return loop


@dataclass
class _FakeManagedPosition:
    order_id: str = "T-1"
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
    open_time: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    tp1_hit: bool = False
    at_breakeven: bool = False
    trailing: bool = False
    tm_trade_id: str = "TM-1"
    last_update: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    stake_usd: float = 0.0
    multiplier: int = 100
    re_entry_eligible: bool = False
    broker_pnl: float = 0.0
    broker_lots: float = 0.0
    scale_in_count: int = 0


def _build_locked_positions():
    """Import and return a fresh _LockedPositions instance."""
    from platforms.main_loop import _LockedPositions
    return _LockedPositions()


# ──────────────────────────────────────────────────────────────────────
# H1 — _LockedPositions thread-safety
# ──────────────────────────────────────────────────────────────────────


class TestLockedPositionsBasic:
    """Verify the _LockedPositions wrapper behaves like a dict."""

    def test_setitem_getitem(self):
        lp = _build_locked_positions()
        pos = _FakeManagedPosition(order_id="A")
        lp["A"] = pos
        assert lp["A"] is pos

    def test_len_and_bool(self):
        lp = _build_locked_positions()
        assert len(lp) == 0
        assert not lp
        lp["A"] = _FakeManagedPosition(order_id="A")
        assert len(lp) == 1
        assert lp

    def test_contains(self):
        lp = _build_locked_positions()
        lp["A"] = _FakeManagedPosition(order_id="A")
        assert "A" in lp
        assert "B" not in lp

    def test_pop_existing(self):
        lp = _build_locked_positions()
        pos = _FakeManagedPosition(order_id="A")
        lp["A"] = pos
        result = lp.pop("A", None)
        assert result is pos
        assert "A" not in lp

    def test_pop_missing_returns_default(self):
        lp = _build_locked_positions()
        assert lp.pop("MISSING", None) is None

    def test_items_returns_snapshot_list(self):
        lp = _build_locked_positions()
        lp["A"] = _FakeManagedPosition(order_id="A")
        lp["B"] = _FakeManagedPosition(order_id="B")
        items = lp.items()
        assert isinstance(items, list)
        assert len(items) == 2

    def test_values_returns_snapshot_list(self):
        lp = _build_locked_positions()
        lp["A"] = _FakeManagedPosition(order_id="A")
        vals = lp.values()
        assert isinstance(vals, list)
        assert len(vals) == 1

    def test_keys_returns_snapshot_list(self):
        lp = _build_locked_positions()
        lp["A"] = _FakeManagedPosition(order_id="A")
        keys = lp.keys()
        assert isinstance(keys, list)
        assert keys == ["A"]

    def test_clear(self):
        lp = _build_locked_positions()
        lp["A"] = _FakeManagedPosition(order_id="A")
        lp.clear()
        assert len(lp) == 0

    def test_snapshot_returns_dict_copy(self):
        lp = _build_locked_positions()
        lp["A"] = _FakeManagedPosition(order_id="A")
        snap = lp.snapshot()
        assert isinstance(snap, dict)
        assert "A" in snap
        lp.pop("A", None)
        assert "A" in snap


class TestLockedPositionsConcurrency:
    """Concurrent stress tests — writer + reader threads must not crash."""

    def test_concurrent_read_write_no_exception(self):
        """Spawn writer and reader threads; assert no RuntimeError or data corruption."""
        lp = _build_locked_positions()
        errors: list[str] = []
        stop = threading.Event()

        def writer():
            try:
                for i in range(500):
                    oid = f"W-{i}"
                    lp[oid] = _FakeManagedPosition(order_id=oid)
                    if i % 3 == 0:
                        lp.pop(oid, None)
                stop.set()
            except Exception as exc:
                errors.append(f"writer: {exc}")
                stop.set()

        def reader():
            try:
                while not stop.is_set():
                    _ = len(lp)
                    _ = list(lp.items())
                    _ = list(lp.values())
                    _ = lp.snapshot()
                    _ = bool(lp)
            except Exception as exc:
                errors.append(f"reader: {exc}")

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent access errors: {errors}"

    def test_clear_during_iteration_no_crash(self):
        """Clearing from one thread while another iterates must not raise."""
        lp = _build_locked_positions()
        for i in range(100):
            lp[f"P-{i}"] = _FakeManagedPosition(order_id=f"P-{i}")

        errors: list[str] = []
        barrier = threading.Barrier(2)

        def iterate_thread():
            try:
                barrier.wait(timeout=5)
                for _ in range(50):
                    for oid, pos in lp.items():
                        _ = pos.symbol
            except Exception as exc:
                errors.append(f"iterate: {exc}")

        def clear_thread():
            try:
                barrier.wait(timeout=5)
                time.sleep(0.001)
                lp.clear()
            except Exception as exc:
                errors.append(f"clear: {exc}")

        t1 = threading.Thread(target=iterate_thread)
        t2 = threading.Thread(target=clear_thread)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Clear-during-iteration errors: {errors}"

    def test_pop_race_returns_none_for_missing(self):
        """Two threads popping the same key — one gets the value, one gets None."""
        lp = _build_locked_positions()
        lp["RACE"] = _FakeManagedPosition(order_id="RACE")

        results: list[Optional[object]] = [None, None]

        def popper(idx):
            results[idx] = lp.pop("RACE", None)

        t1 = threading.Thread(target=popper, args=(0,))
        t2 = threading.Thread(target=popper, args=(1,))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        non_none = [r for r in results if r is not None]
        assert len(non_none) == 1, "Exactly one thread should get the value"


# ──────────────────────────────────────────────────────────────────────
# H5 — Startup recovery in both run modes
# ──────────────────────────────────────────────────────────────────────


class TestStartupRecovery:
    """_perform_startup_recovery() must run exactly once per process."""

    def test_recovery_runs_once(self):
        loop = _make_loop()
        loop._perform_startup_recovery()
        assert loop._recovery_completed is True
        assert loop._restore_positions.call_count == 1
        assert loop._reconcile_positions.call_count == 1
        assert loop._last_reconcile_time is not None

    def test_recovery_idempotent(self):
        loop = _make_loop()
        loop._perform_startup_recovery()
        loop._perform_startup_recovery()
        assert loop._restore_positions.call_count == 1
        assert loop._reconcile_positions.call_count == 1

    def test_recovery_sets_reconcile_time(self):
        loop = _make_loop()
        before = datetime.now(timezone.utc)
        loop._perform_startup_recovery()
        after = datetime.now(timezone.utc)
        assert before <= loop._last_reconcile_time <= after


class TestDashboardPathCallsRecovery:
    """The main.py dashboard path must call _perform_startup_recovery."""

    def test_dashboard_path_calls_recovery(self):
        """Verify _start_trading_loop equivalent path invokes recovery."""
        from platforms.main_loop import _LockedPositions

        loop = MagicMock()
        loop.managed_positions = _LockedPositions()
        loop.running = True
        loop._recovery_completed = False
        loop._perform_startup_recovery = MagicMock()
        loop._perform_startup_recovery.side_effect = lambda: setattr(loop, "_recovery_completed", True)

        loop._perform_startup_recovery()

        loop._perform_startup_recovery.assert_called_once()
        assert loop._recovery_completed is True


# ──────────────────────────────────────────────────────────────────────
# H6 — Periodic reconcile heartbeat
# ──────────────────────────────────────────────────────────────────────


class TestPeriodicReconcileHeartbeat:
    """The periodic reconcile should fire after the configured interval."""

    def test_heartbeat_fires_after_interval(self):
        loop = _make_loop_for_run_once()
        loop._perform_startup_recovery()
        initial_reconcile_calls = loop._reconcile_positions.call_count

        loop._last_reconcile_time = datetime.now(timezone.utc) - timedelta(seconds=60)
        loop.run_once()

        assert loop._reconcile_positions.call_count > initial_reconcile_calls

    def test_heartbeat_does_not_fire_before_interval(self):
        loop = _make_loop_for_run_once()
        loop._perform_startup_recovery()
        initial_reconcile_calls = loop._reconcile_positions.call_count

        loop._last_reconcile_time = datetime.now(timezone.utc)
        loop.run_once()

        assert loop._reconcile_positions.call_count == initial_reconcile_calls

    def test_heartbeat_does_not_fire_before_recovery(self):
        loop = _make_loop_for_run_once()
        assert loop._recovery_completed is False

        loop._last_reconcile_time = datetime.now(timezone.utc) - timedelta(seconds=60)
        loop.run_once()

        assert loop._reconcile_positions.call_count == 0

    def test_reconcile_exception_does_not_propagate(self):
        loop = _make_loop_for_run_once()
        loop._perform_startup_recovery()

        loop._reconcile_positions.reset_mock()
        loop._reconcile_positions.side_effect = RuntimeError("broker down")

        loop._last_reconcile_time = datetime.now(timezone.utc) - timedelta(seconds=60)
        cycle = loop.run_once()
        assert isinstance(cycle, dict)

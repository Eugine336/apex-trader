"""
Test that _position_scores is reseeded from persisted entry scores on restart.

After _restore_positions runs, each reloaded position must have its entry score
seeded into the in-memory _position_scores map so conviction monitoring and
opportunity-cost logic are not silently disabled post-restart.
"""

import contextlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


_PATCHES = [
    "platforms.main_loop.PlatformManager",
    "platforms.main_loop.PairScanner",
    "platforms.main_loop.PairRanker",
    "platforms.main_loop.ScanScheduler",
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
    with contextlib.ExitStack() as stack:
        for target in _PATCHES:
            stack.enter_context(patch(target))
        yield


def _make_loop():
    with _patched_loop_deps():
        from platforms.main_loop import TradingLoop
        loop = TradingLoop()
        return loop


def _make_store_row(order_id="T-1", score=85, symbol="EURUSD"):
    return {
        "order_id": order_id,
        "platform": "mt5",
        "symbol": symbol,
        "direction": "BUY",
        "lots": 0.01,
        "entry_price": 1.10000,
        "sl": 1.09500,
        "tp1": 1.11000,
        "tp2": 1.12000,
        "score": score,
        "regime": "TRENDING",
        "session": "london",
        "entry_type": "FVG",
        "open_time": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "tp1_hit": 0,
        "at_breakeven": 0,
        "trailing": 0,
        "tm_trade_id": "TM-1",
        "last_update": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "stake_usd": 0.0,
        "multiplier": 100,
        "confluences_json": "[]",
    }


def test_restore_positions_seeds_position_scores():
    """After _restore_positions, _position_scores[oid] == [entry_score]."""
    loop = _make_loop()

    row_a = _make_store_row(order_id="T-1", score=85, symbol="EURUSD")
    row_b = _make_store_row(order_id="T-2", score=72, symbol="GBPUSD")
    loop.position_store.load_all_positions = MagicMock(return_value=[row_a, row_b])

    fake_trade = MagicMock()
    fake_trade.trade_id = "TM-FAKE"
    loop.trade_manager.open_trade = MagicMock(return_value=fake_trade)

    loop._restore_positions()

    assert "T-1" in loop._position_scores, "_position_scores missing restored oid T-1"
    assert loop._position_scores["T-1"] == [85], f"expected [85], got {loop._position_scores['T-1']}"
    assert "T-2" in loop._position_scores, "_position_scores missing restored oid T-2"
    assert loop._position_scores["T-2"] == [72], f"expected [72], got {loop._position_scores['T-2']}"


def test_restore_positions_score_zero_is_preserved():
    """A persisted score of 0 (e.g. orphan) is faithfully seeded, not skipped."""
    loop = _make_loop()

    row = _make_store_row(order_id="T-0", score=0, symbol="XAUUSD")
    loop.position_store.load_all_positions = MagicMock(return_value=[row])

    fake_trade = MagicMock()
    fake_trade.trade_id = "TM-FAKE"
    loop.trade_manager.open_trade = MagicMock(return_value=fake_trade)

    loop._restore_positions()

    assert "T-0" in loop._position_scores
    assert loop._position_scores["T-0"] == [0]


def test_empty_store_leaves_position_scores_empty():
    """No persisted positions → _position_scores stays empty."""
    loop = _make_loop()
    loop.position_store.load_all_positions = MagicMock(return_value=[])

    loop._restore_positions()

    assert loop._position_scores == {}

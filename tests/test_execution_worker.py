"""Regression tests for the safety-only PositionWorker."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from inspect import signature

from execution.intents import IntentType
from execution.position_snapshot import PositionSnapshot
from execution.position_worker import MarketContext, PositionWorker, WorkerConfig


NOW = datetime(2025, 6, 15, 20, 45, tzinfo=timezone.utc)


def snap(**overrides):
    values = {
        "order_id": "T-1",
        "platform": "mt5",
        "symbol": "EURUSD",
        "direction": "BUY",
        "entry_price": 1.1000,
        "lots": 0.1,
        "remaining_lots": 0.1,
        "open_time": datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
        "score": 0,
        "sl": 1.0990,
        "sl_original": 1.0990,
        "tp1": 0.0,
        "tp2": 0.0,
        "tp2_original": 0.0,
        "current_price": 1.1050,
        "pnl_pips": 0.0,
        "pnl_dollars": 0.0,
        "pip_size": 0.0001,
        "pip_value_per_lot": 10.0,
        "trade_status": "OPEN",
        "volume_min": 0.01,
        "sl_pending_confirmation": False,
    }
    values.update(overrides)
    return PositionSnapshot(**values)


def sources(intents):
    return {intent.source for intent in intents}


def test_confirmed_hard_stop_closes_long_and_short():
    worker = PositionWorker()
    long_out = worker.evaluate(snap(current_price=1.0989), NOW)
    short_out = worker.evaluate(snap(
        direction="SELL", sl=1.1010, current_price=1.1011,
    ), NOW)
    assert "stop_loss" in sources(long_out)
    assert "stop_loss" in sources(short_out)
    assert all(i.intent_type == IntentType.CLOSE for i in long_out + short_out)


def test_unconfirmed_stop_is_not_used_for_synthetic_stop_hit():
    out = PositionWorker().evaluate(
        snap(current_price=1.0980, sl_pending_confirmation=True), NOW,
    )
    assert "stop_loss" not in sources(out)
    assert not any(i.intent_type == IntentType.CLOSE for i in out)


def test_absolute_profit_protection_only_tightens_a_profitable_position():
    worker = PositionWorker(WorkerConfig(
        absolute_profit_pips=50.0, absolute_profit_usd=100.0,
    ))
    out = worker.evaluate(snap(
        pnl_pips=100.0, current_price=1.1100, sl=1.0990,
    ), NOW)
    intent = next(i for i in out if i.source == "absolute_profit_protection")
    assert intent.intent_type == IntentType.MODIFY_SL
    assert intent.new_sl > 1.0990

    below = worker.evaluate(snap(pnl_pips=40.0), NOW)
    assert "absolute_profit_protection" not in sources(below)


def test_spread_deterioration_tightens_only_when_spread_is_abnormal():
    worker = PositionWorker()
    out = worker.evaluate(
        snap(pnl_pips=10.0),
        NOW,
        market=MarketContext(typical_spread=2.0, current_spread=8.0),
    )
    assert "spread_deterioration" in sources(out)
    normal = worker.evaluate(
        snap(pnl_pips=10.0),
        NOW,
        market=MarketContext(typical_spread=2.0, current_spread=2.5),
    )
    assert "spread_deterioration" not in sources(normal)


def test_session_close_is_operational_not_scan_direction_dependent():
    out = PositionWorker().evaluate(
        snap(symbol="US30", current_price=1.1050), NOW,
    )
    assert "session_close" in sources(out)


def test_dead_zone_protection_requires_real_profit_and_tighter_stop():
    worker = PositionWorker()
    out = worker.evaluate(
        snap(symbol="USDJPY", pnl_pips=10.0, sl=1.0990),
        datetime(2025, 6, 16, 0, 30, tzinfo=timezone.utc),
    )
    assert "dead_zone_protection" in sources(out)

    flat = worker.evaluate(
        snap(symbol="USDJPY", pnl_pips=0.0, sl=1.0990),
        datetime(2025, 6, 16, 0, 30, tzinfo=timezone.utc),
    )
    assert "dead_zone_protection" not in sources(flat)


def test_weekend_protection_is_preserved_for_session_gated_positions():
    worker = PositionWorker()
    friday = datetime(2025, 6, 20, 20, 50, tzinfo=timezone.utc)
    derisk = worker.evaluate(snap(pnl_pips=10.0), friday)
    assert "weekend_protection" in sources(derisk)

    close_worker = PositionWorker(WorkerConfig(weekend_protection_mode="close"))
    close = close_worker.evaluate(snap(), friday)
    assert "weekend_protection" in sources(close)
    assert any(i.intent_type == IntentType.CLOSE for i in close)


def test_weekend_protection_does_not_touch_24_7_synthetics():
    out = PositionWorker().evaluate(
        snap(symbol="1HZ100V", pnl_pips=10.0),
        datetime(2025, 6, 20, 20, 50, tzinfo=timezone.utc),
    )
    assert "weekend_protection" not in sources(out)


def test_terminal_positions_are_not_managed():
    worker = PositionWorker()
    for status in ("CLOSED", "STOPPED", "TIME_EXIT"):
        assert worker.evaluate(snap(trade_status=status), NOW) == []


def test_worker_has_no_discretionary_or_directional_management_surface():
    assert set(signature(PositionWorker.evaluate).parameters) == {
        "self", "snap", "now", "market",
    }
    assert not hasattr(WorkerConfig, "discretionary_exits_enabled")
    assert not hasattr(WorkerConfig, "scan_directional_exits_enabled")
    assert not hasattr(MarketContext, "direction")
    for name in (
        "_check_tp1", "_check_breakeven", "_check_tp2", "_check_tp3",
        "_check_stall", "_check_invalidation", "_check_conviction_collapse",
        "_check_htf_candle_close", "_check_opportunity_cost",
        "_check_structure_trailing", "_check_atr_trailing",
    ):
        assert not hasattr(PositionWorker, name)


def test_worker_is_safe_for_concurrent_read_only_evaluation():
    worker = PositionWorker()
    snapshots = [snap(order_id=f"T-{i}") for i in range(32)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda item: worker.evaluate(item, NOW), snapshots))
    assert len(results) == len(snapshots)

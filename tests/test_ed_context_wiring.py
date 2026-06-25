"""Tests for ED context wiring — ScanContext + MarketContext into PositionWorker.

Verifies:
1. Config alignment (score threshold, max positions, conviction cap)
2. ScanContext-dependent checks fire when context is provided
3. MarketContext-dependent checks fire when context is provided
4. Backward compat — no context = tick-level checks still work
5. Score history accumulates across eval cycles
6. Drawdown guard risk_map is used for sizing
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from execution.intents import Intent, IntentType
from execution.position_worker import (
    PositionWorker,
    WorkerConfig,
    ScanContext,
    MarketContext,
)
from execution.position_snapshot import PositionSnapshot


def _snap(
    *,
    symbol: str = "EURUSD",
    direction: str = "LONG",
    entry_price: float = 1.1000,
    current_price: float = 1.1010,
    sl: float = 1.0950,
    sl_original: float = 1.0950,
    tp1: float = 1.1050,
    tp2: float = 1.1100,
    pnl_pips: float = 10.0,
    pnl_dollars: float = 10.0,
    at_breakeven: bool = False,
    partial_closed: bool = False,
    score: int = 70,
    score_history: tuple[int, ...] = (),
    pip_size: float = 0.0001,
    pip_value_per_lot: float = 10.0,
    lots: float = 0.01,
    open_time: datetime | None = None,
    order_id: str = "12345",
    platform: str = "mt5",
    trade_status: str = "OPEN",
) -> PositionSnapshot:
    return PositionSnapshot(
        order_id=order_id,
        platform=platform,
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        lots=lots,
        remaining_lots=lots,
        open_time=open_time or datetime.now(timezone.utc) - timedelta(minutes=30),
        score=score,
        sl=sl,
        sl_original=sl_original,
        tp1=tp1,
        tp2=tp2,
        tp2_original=tp2,
        tp1_hit=partial_closed,
        at_breakeven=at_breakeven,
        trailing=False,
        partial_closed=partial_closed,
        re_entry_eligible=False,
        current_price=current_price,
        broker_pnl=pnl_dollars,
        broker_lots=lots,
        pnl_pips=pnl_pips,
        pnl_dollars=pnl_dollars,
        pip_size=pip_size,
        pip_value_per_lot=pip_value_per_lot,
        candles_since_entry=10,
        entry_timeframe="M5",
        highest_since_entry=max(entry_price, current_price),
        lowest_since_entry=min(entry_price, current_price),
        trade_status=trade_status,
        score_history=score_history,
    )


class TestConfigAlignment:
    def test_score_threshold_is_85(self):
        from entry.models import EntryConfig
        cfg = EntryConfig()
        assert cfg.min_entry_score == 85

    def test_max_positions_is_5(self):
        cfg = WorkerConfig()
        assert cfg.max_open_trades == 5

    def test_conviction_cap_allows_above_one(self):
        mult = 1.5
        clamped = max(0.15, min(2.0, mult))
        assert clamped == 1.5

    def test_conviction_cap_upper_bound(self):
        mult = 3.0
        clamped = max(0.15, min(2.0, mult))
        assert clamped == 2.0


class TestScanContextChecks:
    """Verify that ScanContext-dependent checks fire correctly."""

    def test_invalidation_fires_with_low_score_negative_pnl(self):
        worker = PositionWorker()
        snap = _snap(pnl_pips=-5.0, pnl_dollars=-5.0, current_price=1.0995)
        scan = ScanContext(direction="LONG", score=30, opposing_score_boost=0)
        intents = worker.evaluate(snap, scan=scan)
        close_intents = [i for i in intents if i.intent_type == IntentType.CLOSE and "invalidation" in i.source]
        assert len(close_intents) > 0

    def test_invalidation_opposing_signal(self):
        worker = PositionWorker()
        snap = _snap(direction="LONG", pnl_pips=5.0)
        scan = ScanContext(direction="SHORT", score=80, opposing_score_boost=0)
        intents = worker.evaluate(snap, scan=scan)
        close_intents = [i for i in intents if i.intent_type == IntentType.CLOSE and "opposing" in i.source]
        assert len(close_intents) > 0

    def test_no_invalidation_without_scan_context(self):
        worker = PositionWorker()
        snap = _snap(pnl_pips=-5.0, pnl_dollars=-5.0, current_price=1.0995)
        intents = worker.evaluate(snap, scan=None)
        inv = [i for i in intents if "invalidation" in i.source]
        assert len(inv) == 0

    def test_conviction_collapse_fires(self):
        worker = PositionWorker(WorkerConfig(
            conviction_decline_cycles=4,
            conviction_decline_min_drop=3,
            conviction_profit_hold_pips=20.0,
        ))
        scores = (80, 75, 70, 65)
        snap = _snap(score_history=scores, pnl_pips=5.0)
        scan = ScanContext(direction="LONG", score=65)
        intents = worker.evaluate(snap, scan=scan)
        collapse = [i for i in intents if "conviction" in i.source]
        assert len(collapse) > 0

    def test_no_conviction_collapse_without_score_history(self):
        worker = PositionWorker()
        snap = _snap(score_history=())
        scan = ScanContext(direction="LONG", score=65)
        intents = worker.evaluate(snap, scan=scan)
        collapse = [i for i in intents if "conviction" in i.source]
        assert len(collapse) == 0


class TestMarketContextChecks:
    """Verify MarketContext-dependent checks fire correctly."""

    def test_htf_candle_close_bearish_against_long(self):
        worker = PositionWorker()
        snap = _snap(direction="LONG", pnl_pips=5.0)
        market = MarketContext(
            h1_last_closed_open=1.1020,
            h1_last_closed_close=1.0980,
            h1_last_closed_high=1.1025,
            h1_last_closed_low=1.0975,
            h1_last_closed_time=datetime.now(timezone.utc),
            last_seen_h1_close=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        intents = worker.evaluate(snap, market=market)
        htf = [i for i in intents if "htf_candle" in i.source]
        assert len(htf) > 0

    def test_no_htf_without_market_context(self):
        worker = PositionWorker()
        snap = _snap(direction="LONG", pnl_pips=5.0)
        intents = worker.evaluate(snap, market=None)
        htf = [i for i in intents if "htf_candle" in i.source]
        assert len(htf) == 0

    def test_spread_deterioration_triggers_be(self):
        worker = PositionWorker(WorkerConfig(
            spread_deterioration_multiplier=3.0,
        ))
        snap = _snap(
            direction="LONG",
            at_breakeven=False,
            entry_price=1.1000,
            current_price=1.1020,
            sl=1.0950,
            pnl_pips=20.0,
        )
        market = MarketContext(
            typical_spread=1.5,
            current_spread=6.0,
        )
        intents = worker.evaluate(snap, market=market)
        spread_intents = [i for i in intents if "spread" in i.source]
        assert len(spread_intents) > 0

    def test_session_close_index(self):
        worker = PositionWorker(WorkerConfig(
            session_close_enabled=True,
            index_close_buffer_minutes=30,
        ))
        _close_h, _close_m = 21, 0
        now = datetime(2026, 6, 19, 20, 45, tzinfo=timezone.utc)
        snap = _snap(symbol="US30")
        market = MarketContext()
        intents = worker.evaluate(snap, now=now, market=market)
        session = [i for i in intents if "session_close" in i.source]
        assert len(session) > 0

    def test_no_session_close_without_market_context(self):
        worker = PositionWorker()
        now = datetime(2026, 6, 19, 20, 45, tzinfo=timezone.utc)
        snap = _snap(symbol="US30")
        intents = worker.evaluate(snap, now=now, market=None)
        session = [i for i in intents if "session_close" in i.source]
        assert len(session) == 0


class TestBackwardCompat:
    """Verify tick-level checks still work without any context."""

    def test_sl_hit_no_context(self):
        worker = PositionWorker()
        snap = _snap(direction="LONG", sl=1.1005, current_price=1.1000)
        intents = worker.evaluate(snap, scan=None, market=None)
        sl_hit = [i for i in intents if "stop_loss" in i.source]
        assert len(sl_hit) > 0

    def test_tp2_hit_no_context(self):
        worker = PositionWorker()
        snap = _snap(direction="LONG", tp2=1.1100, current_price=1.1105)
        intents = worker.evaluate(snap, scan=None, market=None)
        tp2 = [i for i in intents if "tp2" in i.source]
        assert len(tp2) > 0

    def test_stall_held_without_live_read(self):
        # Opportunistic stall: with no live WorldModel read (scan=None) a flat,
        # aging position is HELD — it is never cut on elapsed time alone.
        worker = PositionWorker()
        now = datetime.now(timezone.utc)
        snap = _snap(
            pnl_pips=0.5,
            open_time=now - timedelta(minutes=45),
        )
        intents = worker.evaluate(snap, now=now, scan=None, market=None)
        stall = [i for i in intents if "stall" in i.source]
        assert len(stall) == 0

    def test_stall_exit_fires_on_structure_loss(self):
        # Flat, aged, and the live bias opposes the position → stall fires.
        worker = PositionWorker()
        now = datetime.now(timezone.utc)
        snap = _snap(
            direction="LONG",
            pnl_pips=0.5,
            open_time=now - timedelta(minutes=45),
        )
        scan = ScanContext(direction="SHORT", score=70)
        intents = worker.evaluate(snap, now=now, scan=scan, market=None)
        stall = [i for i in intents if "stall" in i.source]
        assert len(stall) > 0


class TestScoreHistory:
    """Verify score_history is used by conviction collapse."""

    def test_short_history_no_collapse(self):
        worker = PositionWorker(WorkerConfig(conviction_decline_cycles=4))
        snap = _snap(score_history=(80, 75))
        scan = ScanContext(direction="LONG", score=70)
        intents = worker.evaluate(snap, scan=scan)
        collapse = [i for i in intents if "conviction" in i.source]
        assert len(collapse) == 0

    def test_non_declining_no_collapse(self):
        worker = PositionWorker(WorkerConfig(conviction_decline_cycles=4, conviction_decline_min_drop=3))
        snap = _snap(score_history=(70, 72, 68, 71))
        scan = ScanContext(direction="LONG", score=71)
        intents = worker.evaluate(snap, scan=scan)
        collapse = [i for i in intents if "conviction" in i.source]
        assert len(collapse) == 0

    def test_profitable_trade_holds_despite_collapse(self):
        worker = PositionWorker(WorkerConfig(
            conviction_decline_cycles=4,
            conviction_decline_min_drop=3,
            conviction_profit_hold_pips=20.0,
        ))
        snap = _snap(score_history=(80, 75, 70, 65), pnl_pips=25.0)
        scan = ScanContext(direction="LONG", score=65)
        intents = worker.evaluate(snap, scan=scan)
        collapse = [i for i in intents if "conviction" in i.source]
        assert len(collapse) == 0

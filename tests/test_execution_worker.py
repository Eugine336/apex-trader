"""Tests for execution/position_worker.py — PositionWorker management checks."""

import pytest
from datetime import datetime, timedelta, timezone

from execution.intents import IntentType
from execution.position_snapshot import PositionSnapshot
from execution.position_worker import (
    PositionWorker,
    WorkerConfig,
    ScanContext,
    MarketContext,
)


def _snap(
    direction="BUY",
    entry_price=1.10000,
    sl=1.09900,
    sl_original=1.09900,
    tp1=1.11000,
    tp2=1.12000,
    current_price=1.10500,
    pnl_pips=50.0,
    pip_size=0.0001,
    pip_value_per_lot=10.0,
    lots=0.1,
    partial_closed=False,
    at_breakeven=False,
    tp1_hit=False,
    tp3=None,
    tp3_hit=False,
    trade_status="OPEN",
    open_time=None,
    entry_timeframe="M5",
    score=85,
    score_history=(),
    platform="mt5",
    symbol="EURUSD",
    order_id="T1",
    stake_usd=0.0,
    multiplier=100,
    broker_pnl=0.0,
    **kw,
):
    if open_time is None:
        open_time = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    return PositionSnapshot(
        order_id=order_id,
        platform=platform,
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        lots=lots,
        remaining_lots=lots,
        open_time=open_time,
        score=score,
        sl=sl,
        sl_original=sl_original,
        tp1=tp1,
        tp2=tp2,
        tp2_original=tp2,
        tp3=tp3,
        tp1_hit=tp1_hit,
        tp3_hit=tp3_hit,
        at_breakeven=at_breakeven,
        partial_closed=partial_closed,
        current_price=current_price,
        pnl_pips=pnl_pips,
        pip_size=pip_size,
        pip_value_per_lot=pip_value_per_lot,
        trade_status=trade_status,
        entry_timeframe=entry_timeframe,
        score_history=tuple(score_history),
        stake_usd=stake_usd,
        multiplier=multiplier,
        broker_pnl=broker_pnl,
        broker_lots=lots,
        candles_since_entry=0,
        highest_since_entry=current_price,
        lowest_since_entry=entry_price,
        trailing=False,
        re_entry_eligible=False,
        tm_trade_id="tm1",
        confluences=(),
        scale_in_count=0,
        **kw,
    )


NOW = datetime(2025, 6, 15, 14, 0, tzinfo=timezone.utc)


class TestStopLoss:
    def test_long_sl_hit(self):
        snap = _snap(direction="BUY", sl=1.09900, current_price=1.09800)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        assert any(i.intent_type == IntentType.CLOSE and "SL hit" in i.reason for i in intents)

    def test_long_sl_not_hit(self):
        snap = _snap(direction="BUY", sl=1.09900, current_price=1.10000)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        assert not any(i.source == "stop_loss" for i in intents)

    def test_short_sl_hit(self):
        snap = _snap(
            direction="SELL", entry_price=1.10, sl=1.11,
            sl_original=1.11, tp1=1.09, tp2=1.08,
            current_price=1.11100,
        )
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        assert any(i.intent_type == IntentType.CLOSE and "SL hit" in i.reason for i in intents)

    def test_zero_sl_skipped(self):
        snap = _snap(sl=0.0)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        assert not any(i.source == "stop_loss" for i in intents)


class TestTP1:
    def test_long_tp1_hit_mt5(self):
        snap = _snap(tp1=1.105, current_price=1.106, partial_closed=False)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        tp1_intents = [i for i in intents if i.source == "tp1_partial"]
        assert len(tp1_intents) == 1
        assert tp1_intents[0].intent_type == IntentType.PARTIAL_CLOSE
        assert tp1_intents[0].close_fraction == 0.5

    def test_long_tp1_hit_deriv(self):
        snap = _snap(
            tp1=1.105, current_price=1.106, partial_closed=False,
            platform="deriv",
        )
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        deriv_intents = [i for i in intents if i.source == "tp1_deriv"]
        assert len(deriv_intents) == 1
        assert deriv_intents[0].intent_type == IntentType.CLOSE

    def test_tp1_already_hit_skipped(self):
        snap = _snap(tp1=1.105, current_price=1.106, partial_closed=True)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        assert not any(i.source in ("tp1_partial", "tp1_deriv") for i in intents)

    def test_short_tp1_hit(self):
        snap = _snap(
            direction="SELL", entry_price=1.10, sl=1.11,
            sl_original=1.11, tp1=1.09, tp2=1.08,
            current_price=1.089, partial_closed=False,
        )
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        assert any(i.source == "tp1_partial" for i in intents)


class TestBreakeven:
    def test_breakeven_activates_after_tp1(self):
        snap = _snap(
            partial_closed=True, at_breakeven=False,
            pnl_pips=15.0, current_price=1.10150,
        )
        worker = PositionWorker(WorkerConfig(breakeven_min_profit_r=0.5))
        intents = worker.evaluate(snap, NOW)
        be_intents = [i for i in intents if i.source == "breakeven"]
        assert len(be_intents) == 1
        assert be_intents[0].intent_type == IntentType.MODIFY_SL

    def test_breakeven_skipped_if_already_set(self):
        snap = _snap(partial_closed=True, at_breakeven=True, pnl_pips=15.0)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        assert not any(i.source == "breakeven" for i in intents)

    def test_breakeven_skipped_if_not_partial(self):
        snap = _snap(partial_closed=False, pnl_pips=15.0)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        assert not any(i.source == "breakeven" for i in intents)


class TestTP2:
    def test_long_tp2_hit(self):
        snap = _snap(tp2=1.12, current_price=1.121)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        assert any(
            i.intent_type == IntentType.CLOSE and i.source == "tp2_target"
            for i in intents
        )

    def test_tp2_not_hit(self):
        snap = _snap(tp2=1.12, current_price=1.115)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        assert not any(i.source == "tp2_target" for i in intents)


class TestTP3:
    def test_tp3_hit(self):
        cfg = WorkerConfig(tp3_ladder_enabled=True, tp3_close_ratio=0.5)
        snap = _snap(
            tp3=1.15, current_price=1.151, partial_closed=True, tp3_hit=False,
        )
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW)
        assert any(i.source == "tp3_ladder" for i in intents)

    def test_tp3_disabled(self):
        cfg = WorkerConfig(tp3_ladder_enabled=False)
        snap = _snap(tp3=1.15, current_price=1.151, partial_closed=True)
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW)
        assert not any(i.source == "tp3_ladder" for i in intents)


class TestStallExit:
    def test_stall_exit_fires_when_structure_lost(self):
        # Flat, aged, AND live bias has flipped against the LONG → cut.
        open_time = NOW - timedelta(minutes=90)
        snap = _snap(
            direction="BUY", open_time=open_time, pnl_pips=0.5,
            entry_timeframe="M5",
        )
        scan = ScanContext(direction="SHORT", score=70)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert any(i.source == "stall_exit" for i in intents)

    def test_stall_exit_fires_when_conviction_decayed(self):
        # Flat, aged, bias still agrees but conviction decayed below floor → cut.
        open_time = NOW - timedelta(minutes=90)
        snap = _snap(direction="BUY", open_time=open_time, pnl_pips=0.5)
        scan = ScanContext(direction="LONG", score=20)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert any(i.source == "stall_exit" for i in intents)

    def test_no_stall_when_structure_still_supports(self):
        # Flat and aged, but the live WorldModel still supports the thesis →
        # the position is held (market decides, not the clock).
        open_time = NOW - timedelta(minutes=90)
        snap = _snap(direction="BUY", open_time=open_time, pnl_pips=0.5)
        scan = ScanContext(direction="LONG", score=80)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert not any(i.source == "stall_exit" for i in intents)

    def test_no_stall_without_live_read(self):
        # No live WorldModel read available → never cut on elapsed time alone.
        open_time = NOW - timedelta(minutes=90)
        snap = _snap(open_time=open_time, pnl_pips=0.5)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, scan=None)
        assert not any(i.source == "stall_exit" for i in intents)

    def test_no_stall_inside_min_hold_floor(self):
        # Structure lost but the position is younger than the safety floor →
        # held until the brain has had time to re-read it.
        open_time = NOW - timedelta(minutes=10)
        snap = _snap(direction="BUY", open_time=open_time, pnl_pips=0.5)
        scan = ScanContext(direction="SHORT", score=70)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert not any(i.source == "stall_exit" for i in intents)

    def test_stall_exit_skipped_if_profitable(self):
        open_time = NOW - timedelta(minutes=90)
        snap = _snap(open_time=open_time, pnl_pips=50.0)
        scan = ScanContext(direction="SHORT", score=70)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert not any(i.source == "stall_exit" for i in intents)

    def test_stall_exit_skipped_if_partial_closed(self):
        open_time = NOW - timedelta(minutes=90)
        snap = _snap(open_time=open_time, pnl_pips=0.5, partial_closed=True)
        scan = ScanContext(direction="SHORT", score=70)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert not any(i.source == "stall_exit" for i in intents)

    def test_legacy_clock_fallback_when_structure_gate_disabled(self):
        # Opt-out: with the structure gate disabled, the legacy per-timeframe
        # clock still fires (backtest / no-WorldModel environments).
        open_time = NOW - timedelta(minutes=90)
        snap = _snap(open_time=open_time, pnl_pips=0.5, entry_timeframe="M5")
        cfg = WorkerConfig(stall_requires_structure_loss=False)
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW)
        assert any(i.source == "stall_exit" for i in intents)


class TestDynamicSLTightening:
    def test_tighten_fires_at_2r(self):
        cfg = WorkerConfig(
            dynamic_sl_tightening_enabled=True,
            dynamic_sl_tighten_at_r=2.0,
            dynamic_sl_tighten_ratio=0.5,
        )
        snap = _snap(
            at_breakeven=True,
            entry_price=1.10000, sl=1.10000, sl_original=1.09900,
            current_price=1.10250,
        )
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW)
        tighten = [i for i in intents if i.source == "dynamic_sl_tighten"]
        assert len(tighten) == 1
        assert tighten[0].new_sl is not None
        assert tighten[0].new_sl > snap.sl

    def test_tighten_skipped_if_not_at_be(self):
        cfg = WorkerConfig(dynamic_sl_tightening_enabled=True)
        snap = _snap(at_breakeven=False, current_price=1.10250)
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW)
        assert not any(i.source == "dynamic_sl_tighten" for i in intents)

    def test_tighten_disabled(self):
        cfg = WorkerConfig(dynamic_sl_tightening_enabled=False)
        snap = _snap(at_breakeven=True, current_price=1.10250)
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW)
        assert not any(i.source == "dynamic_sl_tighten" for i in intents)


class TestAbsoluteProfitProtection:
    def test_protection_fires(self):
        cfg = WorkerConfig(
            absolute_profit_protection_enabled=True,
            absolute_profit_pips=30.0,
            absolute_profit_usd=50.0,
        )
        snap = _snap(
            pnl_pips=40.0, lots=0.2, pip_value_per_lot=10.0,
            at_breakeven=False,
        )
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW)
        assert any(i.source == "absolute_profit_protection" for i in intents)

    def test_protection_skipped_at_breakeven(self):
        cfg = WorkerConfig(absolute_profit_protection_enabled=True)
        snap = _snap(pnl_pips=60.0, lots=0.2, at_breakeven=True)
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW)
        assert not any(i.source == "absolute_profit_protection" for i in intents)


class TestInvalidation:
    def test_low_score_exits_losing_trade(self):
        snap = _snap(pnl_pips=-5.0)
        scan = ScanContext(direction="SHORT", score=30)
        worker = PositionWorker(WorkerConfig(invalidation_score_threshold=40))
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert any(i.source == "invalidation_low_score" for i in intents)

    def test_low_score_keeps_profitable_trade(self):
        snap = _snap(pnl_pips=10.0)
        scan = ScanContext(direction="SHORT", score=30)
        worker = PositionWorker(WorkerConfig(invalidation_score_threshold=40))
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert not any(i.source == "invalidation_low_score" for i in intents)

    def test_opposing_signal_exits(self):
        snap = _snap(direction="BUY", pnl_pips=-5.0)
        scan = ScanContext(direction="SHORT", score=80)
        worker = PositionWorker(WorkerConfig(opposing_signal_threshold=75))
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert any(i.source == "invalidation_opposing" for i in intents)

    def test_same_direction_no_exit(self):
        snap = _snap(direction="BUY")
        scan = ScanContext(direction="LONG", score=80)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert not any("invalidation" in i.source for i in intents)


class TestEntryGracePeriod:
    """Discretionary exits must be suppressed inside the entry grace window."""

    def test_fresh_position_skips_invalidation(self):
        # Position opened 2s ago, underwater, low score — invalidation would
        # normally fire, but the grace window must suppress it.
        open_t = NOW - timedelta(seconds=2)
        snap = _snap(pnl_pips=-1.0, open_time=open_t)
        scan = ScanContext(direction="SHORT", score=30)
        worker = PositionWorker(WorkerConfig(min_hold_seconds=120.0))
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert not any("invalidation" in i.source for i in intents)

    def test_aged_position_allows_invalidation(self):
        # Same underwater low-score position, but well past the grace window.
        open_t = NOW - timedelta(seconds=200)
        snap = _snap(pnl_pips=-1.0, open_time=open_t)
        scan = ScanContext(direction="SHORT", score=30)
        worker = PositionWorker(WorkerConfig(min_hold_seconds=120.0))
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert any(i.source == "invalidation_low_score" for i in intents)

    def test_stop_loss_fires_inside_grace(self):
        # Hard safety (SL) must NEVER be gated by the grace window.
        open_t = NOW - timedelta(seconds=2)
        snap = _snap(
            direction="BUY", sl=1.09900, current_price=1.09800,
            open_time=open_t,
        )
        scan = ScanContext(direction="SHORT", score=30)
        worker = PositionWorker(WorkerConfig(min_hold_seconds=120.0))
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert any(i.source == "stop_loss" for i in intents)


class TestConvictionCollapse:
    def test_declining_scores_exits(self):
        snap = _snap(
            pnl_pips=5.0,
            score_history=(90, 86, 82, 78),
        )
        cfg = WorkerConfig(
            conviction_decline_cycles=4,
            conviction_decline_min_drop=3,
            conviction_profit_hold_pips=20.0,
        )
        scan = ScanContext(direction="LONG", score=78)
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert any(i.source == "conviction_collapse" for i in intents)

    def test_profitable_trade_holds(self):
        snap = _snap(
            pnl_pips=25.0,
            score_history=(90, 86, 82, 78),
        )
        cfg = WorkerConfig(conviction_profit_hold_pips=20.0)
        scan = ScanContext(direction="LONG", score=78)
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert not any(i.source == "conviction_collapse" for i in intents)

    def test_insufficient_history_skipped(self):
        snap = _snap(score_history=(90, 86))
        scan = ScanContext(direction="LONG", score=86)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, scan=scan)
        assert not any(i.source == "conviction_collapse" for i in intents)


class TestHTFCandleClose:
    def test_opposing_h1_close_exits(self):
        snap = _snap(direction="BUY", pnl_pips=5.0)
        market = MarketContext(
            h1_last_closed_open=1.1050,
            h1_last_closed_close=1.1020,
            h1_last_closed_high=1.1060,
            h1_last_closed_low=1.1010,
            h1_last_closed_time=datetime(2025, 6, 15, 13, 0, tzinfo=timezone.utc),
            last_seen_h1_close=datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
        )
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, market=market)
        assert any(i.source == "htf_candle_close" for i in intents)

    def test_same_direction_h1_no_exit(self):
        snap = _snap(direction="BUY", pnl_pips=5.0)
        market = MarketContext(
            h1_last_closed_open=1.1020,
            h1_last_closed_close=1.1050,
            h1_last_closed_high=1.1060,
            h1_last_closed_low=1.1010,
            h1_last_closed_time=datetime(2025, 6, 15, 13, 0, tzinfo=timezone.utc),
            last_seen_h1_close=datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
        )
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, market=market)
        assert not any(i.source == "htf_candle_close" for i in intents)

    def test_profitable_trade_holds(self):
        snap = _snap(direction="BUY", pnl_pips=35.0)
        market = MarketContext(
            h1_last_closed_open=1.1050,
            h1_last_closed_close=1.1020,
            h1_last_closed_high=1.1060,
            h1_last_closed_low=1.1010,
            h1_last_closed_time=datetime(2025, 6, 15, 13, 0, tzinfo=timezone.utc),
            last_seen_h1_close=datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
        )
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, market=market)
        assert not any(i.source == "htf_candle_close" for i in intents)


class TestSpreadDeterioration:
    def test_wide_spread_protects(self):
        snap = _snap(at_breakeven=False, entry_price=1.10, sl=1.098)
        market = MarketContext(typical_spread=0.0002, current_spread=0.0008)
        cfg = WorkerConfig(
            spread_monitor_enabled=True,
            spread_deterioration_multiplier=3.0,
        )
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW, market=market)
        assert any(i.source == "spread_deterioration" for i in intents)

    def test_normal_spread_no_action(self):
        snap = _snap(at_breakeven=False)
        market = MarketContext(typical_spread=0.0002, current_spread=0.0003)
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW, market=market)
        assert not any(i.source == "spread_deterioration" for i in intents)


class TestSessionClose:
    def test_index_close_exits(self):
        snap = _snap(symbol="US500")
        close_now = datetime(2025, 6, 15, 20, 45, tzinfo=timezone.utc)
        cfg = WorkerConfig(
            session_close_enabled=True,
            index_close_buffer_minutes=30,
        )
        market = MarketContext()
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, close_now, market=market)
        assert any(i.source == "session_close" for i in intents)

    def test_non_index_no_close(self):
        snap = _snap(symbol="EURUSD")
        close_now = datetime(2025, 6, 15, 20, 45, tzinfo=timezone.utc)
        market = MarketContext()
        worker = PositionWorker()
        intents = worker.evaluate(snap, close_now, market=market)
        assert not any(i.source == "session_close" for i in intents)

    def test_dead_zone_protection(self):
        snap = _snap(symbol="USDJPY", at_breakeven=False)
        dead_zone_time = datetime(2025, 6, 15, 0, 30, tzinfo=timezone.utc)
        cfg = WorkerConfig(
            session_close_enabled=True,
            dead_zone_management=True,
        )
        market = MarketContext()
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, dead_zone_time, market=market)
        assert any(i.source == "dead_zone_protection" for i in intents)


class TestOpportunityCost:
    def test_opportunity_cost_exits(self):
        open_time = NOW - timedelta(minutes=90)
        snap = _snap(
            open_time=open_time, pnl_pips=2.0, score=70,
            partial_closed=False,
        )
        market = MarketContext(
            blocked_candidate={"pair": "GBPUSD", "score": 92, "direction": "LONG"},
        )
        cfg = WorkerConfig(
            opportunity_cost_exit_mode="active",
            opportunity_cost_min_hold_minutes=60.0,
            opportunity_cost_max_pnl_pips=5.0,
            opportunity_cost_score_margin=15,
            max_open_trades=8,
        )
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW, market=market)
        assert any(i.source == "opportunity_cost" for i in intents)

    def test_opportunity_cost_off(self):
        snap = _snap(pnl_pips=2.0, score=70)
        market = MarketContext(
            blocked_candidate={"pair": "GBPUSD", "score": 92, "direction": "LONG"},
        )
        cfg = WorkerConfig(opportunity_cost_exit_mode="off")
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW, market=market)
        assert not any(i.source == "opportunity_cost" for i in intents)


class TestTerminalStatus:
    def test_closed_position_returns_empty(self):
        snap = _snap(trade_status="CLOSED")
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        assert intents == []

    def test_stopped_position_returns_empty(self):
        snap = _snap(trade_status="STOPPED")
        worker = PositionWorker()
        intents = worker.evaluate(snap, NOW)
        assert intents == []


class TestMultipleIntents:
    def test_can_return_multiple_intents(self):
        cfg = WorkerConfig(
            dynamic_sl_tightening_enabled=True,
            dynamic_sl_tighten_at_r=2.0,
            dynamic_sl_tighten_ratio=0.5,
            absolute_profit_protection_enabled=True,
            absolute_profit_pips=30.0,
            absolute_profit_usd=20.0,
        )
        snap = _snap(
            at_breakeven=True,
            entry_price=1.10000, sl=1.10000, sl_original=1.09900,
            current_price=1.10350,
            pnl_pips=35.0, lots=0.1, pip_value_per_lot=10.0,
        )
        worker = PositionWorker(cfg)
        intents = worker.evaluate(snap, NOW)
        sources = {i.source for i in intents}
        assert "dynamic_sl_tighten" in sources


class TestThreadSafety:
    def test_concurrent_workers(self):
        import concurrent.futures

        worker = PositionWorker()
        snaps = [
            _snap(
                order_id=f"T{i}", symbol=f"SYM{i}",
                current_price=1.1 + i * 0.001,
            )
            for i in range(20)
        ]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(worker.evaluate, snap, NOW)
                for snap in snaps
            ]
            results = [f.result() for f in futures]

        assert len(results) == 20
        for r in results:
            assert isinstance(r, list)

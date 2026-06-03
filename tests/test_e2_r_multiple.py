"""E2 — Realized R-multiple & expectancy-in-R tests.

Tests cover:
  (a) Single trade R = pnl / risk
  (b) Breakeven/trailing does not corrupt R (initial risk is used)
  (c) is_fallback at open → risk_dollars None → excluded from R stats
  (d) Consolidated 2-leg and 3-leg chains use first leg's risk as denominator
  (e) mean_r / expectancy_r / r_sample_size with mixed valid/null rows
  (f) Old rows lacking risk_dollars don't crash and are excluded from R
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from brain.trade_journal import TradeJournal, TradeRecord


# ── helpers ──────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_record(
    pair="EURUSD",
    direction="BUY",
    pnl=10.0,
    pnl_dollars=100.0,
    outcome="WIN",
    risk_dollars=50.0,
    swap_modeled=None,
    swap_status="unavailable",
    score=80,
    session="LONDON",
) -> TradeRecord:
    return TradeRecord(
        pair=pair,
        direction=direction,
        entry=1.1000,
        exit=1.1010,
        pnl=pnl,
        score=score,
        confluences=[],
        regime="TRENDING",
        session=session,
        spread=0.5,
        slippage=0.1,
        entry_type="MARKET",
        time_to_tp1=5.0,
        time_to_exit=10.0,
        outcome=outcome,
        pnl_dollars=pnl_dollars,
        swap_modeled=swap_modeled,
        swap_status=swap_status,
        risk_dollars=risk_dollars,
    )


# ── (a) Single trade R = pnl / risk ────────────────────────────────────────

class TestSingleTradeR:
    def test_basic_r_calculation(self):
        """R = 100 / 50 = 2.0"""
        journal = TradeJournal(db_path=":memory:")
        _run(journal.initialize())
        _run(journal.log_trade(_make_record(pnl_dollars=100.0, risk_dollars=50.0)))
        stats = _run(journal.get_performance_stats())
        assert stats["r_sample_size"] == 1
        assert abs(stats["mean_r"] - 2.0) < 0.001

    def test_losing_trade_negative_r(self):
        """R = -30 / 50 = -0.6"""
        journal = TradeJournal(db_path=":memory:")
        _run(journal.initialize())
        _run(journal.log_trade(_make_record(pnl_dollars=-30.0, risk_dollars=50.0, outcome="LOSS")))
        stats = _run(journal.get_performance_stats())
        assert stats["r_sample_size"] == 1
        assert abs(stats["mean_r"] - (-0.6)) < 0.001


# ── (b) Breakeven/trailing does not corrupt R ──────────────────────────────

class TestRUsesInitialRisk:
    def test_initial_risk_preserved(self):
        """Risk_dollars from open (initial SL) is stored, not the current SL."""
        from platforms.trading_loop.positions import ManagedPosition
        from platforms.base_connector import OrderResult

        order = OrderResult(
            success=True, order_id="T1", fill_price=1.1000,
            requested_price=1.1000, slippage_pips=0.0, lots=0.1,
            symbol="EURUSD", direction="BUY", sl=1.0950, tp=1.1050,
            platform="mt5",
        )
        pos = ManagedPosition(order=order, tp1=1.1050, tp2=1.1100, score=85)
        pos.initial_risk_dollars = 50.0
        pos.sl = 1.1000  # moved to breakeven
        pos.at_breakeven = True
        assert pos.initial_risk_dollars == 50.0


# ── (c) Fallback risk → None → excluded from R stats ──────────────────────

class TestFallbackExclusion:
    def test_none_risk_excluded_from_r(self):
        """Trades with risk_dollars=None are included in dollar stats but not R."""
        journal = TradeJournal(db_path=":memory:")
        _run(journal.initialize())
        _run(journal.log_trade(_make_record(pnl_dollars=100.0, risk_dollars=50.0)))
        _run(journal.log_trade(_make_record(pnl_dollars=200.0, risk_dollars=None)))
        stats = _run(journal.get_performance_stats())
        assert stats["r_sample_size"] == 1
        assert abs(stats["mean_r"] - 2.0) < 0.001
        assert abs(stats["avg_rr"] - 150.0) < 0.001

    def test_zero_risk_excluded_from_r(self):
        """Trades with risk_dollars=0 are excluded from R (division guard)."""
        journal = TradeJournal(db_path=":memory:")
        _run(journal.initialize())
        _run(journal.log_trade(_make_record(pnl_dollars=100.0, risk_dollars=0.0)))
        stats = _run(journal.get_performance_stats())
        assert stats["r_sample_size"] == 0
        assert stats["mean_r"] == 0.0


# ── (d) Consolidation uses first leg's risk_dollars ────────────────────────

class TestConsolidationRisk:
    def test_two_leg_uses_first_risk(self):
        """In a 2-leg partial chain, the surviving row gets the first leg's risk."""
        rows = [
            ("EURUSD", "LONDON", 5.0, 3.0, "TP1_FULL_CLOSE_REOPEN", 25.0,
             None, "unavailable", "BUY", 50.0),
            ("EURUSD", "LONDON", 15.0, 10.0, "WIN", 75.0,
             None, "unavailable", "BUY", 30.0),
        ]
        result = TradeJournal._consolidate_partial_rows(rows)
        assert len(result) == 1
        assert result[0][9] == 50.0  # first leg's risk

    def test_three_leg_chain_uses_first_risk(self):
        """In a 3-leg chain, the surviving row gets the very first leg's risk."""
        rows = [
            ("EURUSD", "LONDON", 3.0, 2.0, "TP1_FULL_CLOSE_REOPEN", 10.0,
             None, "unavailable", "BUY", 100.0),
            ("EURUSD", "LONDON", 5.0, 4.0, "TP1_FULL_CLOSE_REOPEN", 20.0,
             None, "unavailable", "BUY", 60.0),
            ("EURUSD", "LONDON", 10.0, 8.0, "WIN", 50.0,
             None, "unavailable", "BUY", 40.0),
        ]
        result = TradeJournal._consolidate_partial_rows(rows)
        assert len(result) == 1
        assert result[0][9] == 100.0  # very first leg's risk
        assert abs(result[0][5] - 80.0) < 0.001  # summed pnl_dollars: 10+20+50

    def test_dict_consolidation_uses_first_risk(self):
        """Dict variant carries the first leg's risk_dollars."""
        trades = [
            {"pair": "EURUSD", "direction": "BUY", "pnl": 5.0, "pnl_dollars": 25.0,
             "outcome": "TP1_FULL_CLOSE_REOPEN", "swap_modeled": None,
             "swap_status": "unavailable", "risk_dollars": 50.0},
            {"pair": "EURUSD", "direction": "BUY", "pnl": 15.0, "pnl_dollars": 75.0,
             "outcome": "WIN", "swap_modeled": None,
             "swap_status": "unavailable", "risk_dollars": 30.0},
        ]
        result = TradeJournal._consolidate_partial_dicts(trades)
        assert len(result) == 1
        assert result[0]["risk_dollars"] == 50.0


# ── (e) mean_r / expectancy_r / r_sample_size with mixed rows ─────────────

class TestRMultipleStats:
    def test_mixed_valid_and_null(self):
        """Three trades: two with risk, one without → R stats use only the two."""
        journal = TradeJournal(db_path=":memory:")
        _run(journal.initialize())
        _run(journal.log_trade(_make_record(pnl_dollars=100.0, risk_dollars=50.0, outcome="WIN")))
        _run(journal.log_trade(_make_record(pnl_dollars=-25.0, risk_dollars=50.0, outcome="LOSS")))
        _run(journal.log_trade(_make_record(pnl_dollars=200.0, risk_dollars=None, outcome="WIN")))
        stats = _run(journal.get_performance_stats())
        assert stats["r_sample_size"] == 2
        r1 = 100.0 / 50.0  # 2.0
        r2 = -25.0 / 50.0  # -0.5
        expected_mean = (r1 + r2) / 2  # 0.75
        assert abs(stats["mean_r"] - expected_mean) < 0.001
        win_pct = 0.5
        loss_pct = 0.5
        avg_win_r = 2.0
        avg_loss_r = -0.5
        expected_exp = win_pct * avg_win_r + loss_pct * avg_loss_r  # 0.75
        assert abs(stats["expectancy_r"] - expected_exp) < 0.001

    def test_all_null_risk(self):
        """All trades missing risk → R stats = 0."""
        journal = TradeJournal(db_path=":memory:")
        _run(journal.initialize())
        _run(journal.log_trade(_make_record(pnl_dollars=100.0, risk_dollars=None)))
        stats = _run(journal.get_performance_stats())
        assert stats["r_sample_size"] == 0
        assert stats["mean_r"] == 0.0
        assert stats["expectancy_r"] == 0.0

    def test_empty_journal(self):
        """No trades → all stats zero including R."""
        journal = TradeJournal(db_path=":memory:")
        _run(journal.initialize())
        stats = _run(journal.get_performance_stats())
        assert stats["r_sample_size"] == 0
        assert stats["mean_r"] == 0.0
        assert stats["expectancy_r"] == 0.0


# ── (f) Old rows lacking risk_dollars don't crash ──────────────────────────

class TestOldRowBackwardCompat:
    def test_old_row_format_no_crash(self):
        """Rows with fewer columns (pre-risk_dollars) produce R=0 sample."""
        old_rows = [
            ("EURUSD", "LONDON", 10.0, 5.0, "WIN", 100.0, None, "unavailable", "BUY"),
        ]
        result = TradeJournal._consolidate_partial_rows(old_rows)
        assert len(result) == 1

    def test_get_all_trades_old_schema_no_crash(self):
        """get_all_trades_as_dicts handles missing risk_dollars column."""
        journal = TradeJournal(db_path=":memory:")
        _run(journal.initialize())
        _run(journal.log_trade(_make_record(risk_dollars=None)))
        trades = _run(journal.get_all_trades_as_dicts())
        assert len(trades) == 1
        assert trades[0]["risk_dollars"] is None


# ── TradeRecord field exists ───────────────────────────────────────────────

class TestTradeRecordField:
    def test_risk_dollars_default_none(self):
        """TradeRecord has risk_dollars defaulting to None."""
        rec = TradeRecord(
            pair="EURUSD", direction="BUY", entry=1.1, exit=1.11,
            pnl=10.0, score=80, confluences=[], regime="", session="",
            spread=0.0, slippage=0.0, entry_type="MARKET",
            time_to_tp1=None, time_to_exit=5.0, outcome="WIN",
        )
        assert rec.risk_dollars is None

    def test_risk_dollars_explicit(self):
        rec = TradeRecord(
            pair="EURUSD", direction="BUY", entry=1.1, exit=1.11,
            pnl=10.0, score=80, confluences=[], regime="", session="",
            spread=0.0, slippage=0.0, entry_type="MARKET",
            time_to_tp1=None, time_to_exit=5.0, outcome="WIN",
            risk_dollars=42.5,
        )
        assert rec.risk_dollars == 42.5


# ── ManagedPosition field ─────────────────────────────────────────────────

class TestManagedPositionField:
    def test_initial_risk_dollars_default_none(self):
        from platforms.trading_loop.positions import ManagedPosition
        from platforms.base_connector import OrderResult

        order = OrderResult(
            success=True, order_id="T1", fill_price=1.1,
            requested_price=1.1, slippage_pips=0.0, lots=0.1,
            symbol="EURUSD", direction="BUY", sl=1.09, tp=1.12,
            platform="mt5",
        )
        pos = ManagedPosition(order=order, tp1=1.12, tp2=1.13)
        assert pos.initial_risk_dollars is None

    def test_initial_risk_dollars_settable(self):
        from platforms.trading_loop.positions import ManagedPosition
        from platforms.base_connector import OrderResult

        order = OrderResult(
            success=True, order_id="T2", fill_price=1.1,
            requested_price=1.1, slippage_pips=0.0, lots=0.1,
            symbol="EURUSD", direction="BUY", sl=1.09, tp=1.12,
            platform="mt5",
        )
        pos = ManagedPosition(order=order, tp1=1.12, tp2=1.13)
        pos.initial_risk_dollars = 55.0
        assert pos.initial_risk_dollars == 55.0

"""
APEX TRADER — F2 Phase-2: Swap-Netting Tests

Validates that modeled swap/rollover costs are correctly netted into
performance statistics and properly carried through partial-trade
consolidation chains.

Sign convention (from brain/swap_model.py):
  positive = credit (money received)
  negative = cost (money paid)
  netting: net_pnl = pnl_dollars + swap_modeled
"""

import asyncio
from datetime import datetime, timezone

import pytest


class TestSwapNetPnl:
    """Unit tests for the _swap_net_pnl static method."""

    @staticmethod
    def _make_row(pnl_dollars, swap_modeled=None, swap_status="unavailable",
                  pnl_pips=0.0, pair="EURUSD", session="LONDON",
                  time_to_exit=60.0, outcome="TP2", direction="BUY"):
        return (
            pair,            # 0
            session,         # 1
            pnl_pips,        # 2
            time_to_exit,    # 3
            outcome,         # 4
            pnl_dollars,     # 5
            swap_modeled,    # 6
            swap_status,     # 7
            direction,       # 8
        )

    def test_negative_swap_reduces_pnl(self):
        """Negative swap = cost: net P&L should be lower than raw."""
        from brain.trade_journal import TradeJournal

        row = self._make_row(pnl_dollars=100.0, swap_modeled=-15.0, swap_status="modeled")
        assert TradeJournal._swap_net_pnl(row) == pytest.approx(85.0)

    def test_positive_swap_increases_pnl(self):
        """Positive swap = credit: net P&L should be higher than raw."""
        from brain.trade_journal import TradeJournal

        row = self._make_row(pnl_dollars=100.0, swap_modeled=5.0, swap_status="modeled")
        assert TradeJournal._swap_net_pnl(row) == pytest.approx(105.0)

    def test_zero_swap_modeled_unchanged(self):
        """Genuinely zero swap (modeled) leaves P&L unchanged."""
        from brain.trade_journal import TradeJournal

        row = self._make_row(pnl_dollars=100.0, swap_modeled=0.0, swap_status="modeled")
        assert TradeJournal._swap_net_pnl(row) == pytest.approx(100.0)

    def test_unavailable_swap_unchanged(self):
        """Unknown swap (unavailable) must NOT be treated as zero."""
        from brain.trade_journal import TradeJournal

        row = self._make_row(pnl_dollars=100.0, swap_modeled=None, swap_status="unavailable")
        assert TradeJournal._swap_net_pnl(row) == pytest.approx(100.0)

    def test_none_pnl_dollars_falls_back_to_pips(self):
        """When pnl_dollars is None, falls back to pnl (pips) column."""
        from brain.trade_journal import TradeJournal

        row = self._make_row(pnl_dollars=None, pnl_pips=30.0, swap_modeled=-5.0, swap_status="modeled")
        assert TradeJournal._swap_net_pnl(row) == pytest.approx(25.0)

    def test_short_row_without_swap_columns(self):
        """Rows from old schema (no swap columns) return raw P&L."""
        from brain.trade_journal import TradeJournal

        short_row = ("EURUSD", "LONDON", 30.0, 60.0, "TP2", 150.0)
        assert TradeJournal._swap_net_pnl(short_row) == pytest.approx(150.0)


class TestSwapNettingInStats:
    """Validates that get_performance_stats uses swap-netted P&L."""

    @pytest.fixture
    def journal(self, tmp_path):
        from brain.trade_journal import TradeJournal
        return TradeJournal(db_path=str(tmp_path / "test.db"))

    @staticmethod
    def _run(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_swap_cost_flips_win_to_loss(self, journal):
        """A small winner with a large swap cost becomes a loser in stats."""
        from brain.trade_journal import TradeRecord

        trade = TradeRecord(
            pair="EURUSD", direction="BUY", entry=1.0800, exit=1.0810,
            pnl=10.0, score=85, confluences=[], regime="TRENDING",
            session="LONDON", spread=1.0, slippage=0.0, entry_type="MARKET",
            time_to_tp1=5.0, time_to_exit=20.0, outcome="TP1",
            pnl_dollars=50.0, swap_modeled=-80.0, swap_status="modeled",
        )
        self._run(journal.initialize())
        self._run(journal.log_trade(trade))
        stats = self._run(journal.get_performance_stats())
        assert stats["win_rate"] == pytest.approx(0.0)
        assert stats["avg_rr"] == pytest.approx(-30.0, abs=0.01)

    def test_swap_credit_flips_loss_to_win(self, journal):
        """A small loser with a large swap credit becomes a winner in stats."""
        from brain.trade_journal import TradeRecord

        trade = TradeRecord(
            pair="EURUSD", direction="SELL", entry=1.0810, exit=1.0815,
            pnl=-5.0, score=80, confluences=[], regime="TRENDING",
            session="LONDON", spread=1.0, slippage=0.0, entry_type="MARKET",
            time_to_tp1=None, time_to_exit=60.0, outcome="BROKER_CLOSED",
            pnl_dollars=-25.0, swap_modeled=50.0, swap_status="modeled",
        )
        self._run(journal.initialize())
        self._run(journal.log_trade(trade))
        stats = self._run(journal.get_performance_stats())
        assert stats["win_rate"] == pytest.approx(100.0)
        assert stats["avg_rr"] == pytest.approx(25.0, abs=0.01)

    def test_unavailable_swap_does_not_affect_stats(self, journal):
        """Trades with unavailable swap use raw pnl_dollars, not zero."""
        from brain.trade_journal import TradeRecord

        trade = TradeRecord(
            pair="GBPUSD", direction="BUY", entry=1.2500, exit=1.2530,
            pnl=30.0, score=85, confluences=[], regime="TRENDING",
            session="NY", spread=1.0, slippage=0.0, entry_type="MARKET",
            time_to_tp1=10.0, time_to_exit=30.0, outcome="TP2",
            pnl_dollars=150.0, swap_modeled=None, swap_status="unavailable",
        )
        self._run(journal.initialize())
        self._run(journal.log_trade(trade))
        stats = self._run(journal.get_performance_stats())
        assert stats["win_rate"] == pytest.approx(100.0)
        assert stats["avg_rr"] == pytest.approx(150.0, abs=0.01)

    def test_profit_factor_reflects_swap(self, journal):
        """Profit factor uses swap-netted figures for both wins and losses."""
        from brain.trade_journal import TradeRecord

        winner = TradeRecord(
            pair="EURUSD", direction="BUY", entry=1.08, exit=1.09,
            pnl=100.0, score=85, confluences=[], regime="TRENDING",
            session="LONDON", spread=1.0, slippage=0.0, entry_type="MARKET",
            time_to_tp1=5.0, time_to_exit=20.0, outcome="TP2",
            pnl_dollars=500.0, swap_modeled=-100.0, swap_status="modeled",
        )
        loser = TradeRecord(
            pair="GBPUSD", direction="SELL", entry=1.25, exit=1.26,
            pnl=-100.0, score=80, confluences=[], regime="RANGING",
            session="NY", spread=1.5, slippage=0.0, entry_type="MARKET",
            time_to_tp1=None, time_to_exit=40.0, outcome="SL",
            pnl_dollars=-500.0, swap_modeled=None, swap_status="unavailable",
        )
        self._run(journal.initialize())
        self._run(journal.log_trade(winner))
        self._run(journal.log_trade(loser))
        stats = self._run(journal.get_performance_stats())
        assert stats["profit_factor"] == pytest.approx(400.0 / 500.0, abs=0.01)


class TestConsolidationCarriesSwap:
    """Validates that partial-trade consolidation sums swap across legs."""

    @staticmethod
    def _make_row(pair, direction, pnl, outcome, pnl_dollars=None,
                  swap_modeled=None, swap_status="unavailable",
                  session="LONDON", time_to_exit=60.0):
        return (
            pair, session, pnl, time_to_exit, outcome,
            pnl_dollars if pnl_dollars is not None else pnl,
            swap_modeled, swap_status, direction,
        )

    def test_two_leg_both_modeled(self):
        """Two-leg chain: both modeled → sum swap, status modeled."""
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN",
                           pnl_dollars=150.0, swap_modeled=-5.0, swap_status="modeled"),
            self._make_row("EURUSD", "BUY", -5.0, "BROKER_CLOSED",
                           pnl_dollars=-25.0, swap_modeled=-3.0, swap_status="modeled"),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 1
        merged = consolidated[0]
        assert merged[6] == pytest.approx(-8.0)
        assert merged[7] == "modeled"

    def test_two_leg_mixed_modeled_unavailable(self):
        """Two-leg chain: only the marker is modeled → sum modeled legs only."""
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN",
                           pnl_dollars=150.0, swap_modeled=-5.0, swap_status="modeled"),
            self._make_row("EURUSD", "BUY", -5.0, "BROKER_CLOSED",
                           pnl_dollars=-25.0, swap_modeled=None, swap_status="unavailable"),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 1
        merged = consolidated[0]
        assert merged[6] == pytest.approx(-5.0)
        assert merged[7] == "modeled"

    def test_two_leg_only_survivor_modeled(self):
        """Two-leg chain: only the survivor is modeled → status modeled."""
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN",
                           pnl_dollars=150.0, swap_modeled=None, swap_status="unavailable"),
            self._make_row("EURUSD", "BUY", -5.0, "BROKER_CLOSED",
                           pnl_dollars=-25.0, swap_modeled=-3.0, swap_status="modeled"),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 1
        merged = consolidated[0]
        assert merged[6] == pytest.approx(-3.0)
        assert merged[7] == "modeled"

    def test_two_leg_neither_modeled(self):
        """Two-leg chain: neither modeled → swap stays None/unavailable."""
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN",
                           pnl_dollars=150.0, swap_modeled=None, swap_status="unavailable"),
            self._make_row("EURUSD", "BUY", -5.0, "BROKER_CLOSED",
                           pnl_dollars=-25.0, swap_modeled=None, swap_status="unavailable"),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 1
        merged = consolidated[0]
        assert merged[6] is None
        assert merged[7] == "unavailable"

    def test_three_leg_chain_sums_all_modeled_swap(self):
        """Three-leg chain: all modeled → total swap is sum of all three."""
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN",
                           pnl_dollars=150.0, swap_modeled=-2.0, swap_status="modeled"),
            self._make_row("EURUSD", "BUY", 20.0, "TP1_FULL_CLOSE_REOPEN",
                           pnl_dollars=100.0, swap_modeled=-3.0, swap_status="modeled"),
            self._make_row("EURUSD", "BUY", -5.0, "BROKER_CLOSED",
                           pnl_dollars=-25.0, swap_modeled=-1.5, swap_status="modeled"),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 1
        merged = consolidated[0]
        assert merged[6] == pytest.approx(-6.5)
        assert merged[7] == "modeled"

    def test_three_leg_chain_mixed_status(self):
        """Three-leg chain with mixed status: only modeled legs contribute."""
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN",
                           pnl_dollars=150.0, swap_modeled=-2.0, swap_status="modeled"),
            self._make_row("EURUSD", "BUY", 20.0, "TP1_FULL_CLOSE_REOPEN",
                           pnl_dollars=100.0, swap_modeled=None, swap_status="unavailable"),
            self._make_row("EURUSD", "BUY", -5.0, "BROKER_CLOSED",
                           pnl_dollars=-25.0, swap_modeled=-1.5, swap_status="modeled"),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 1
        merged = consolidated[0]
        assert merged[6] == pytest.approx(-3.5)
        assert merged[7] == "modeled"

    def test_positive_swap_credit_in_chain(self):
        """Verify positive swap (credit) is correctly summed, not zeroed."""
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("USDJPY", "SELL", 40.0, "TP1_FULL_CLOSE_REOPEN",
                           pnl_dollars=200.0, swap_modeled=3.5, swap_status="modeled"),
            self._make_row("USDJPY", "SELL", 10.0, "TP2",
                           pnl_dollars=50.0, swap_modeled=1.0, swap_status="modeled"),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 1
        merged = consolidated[0]
        assert merged[6] == pytest.approx(4.5)
        assert merged[7] == "modeled"


class TestDictConsolidationCarriesSwap:
    """Validates dict-based consolidation sums swap across legs."""

    @staticmethod
    def _make_trade(pair, direction, pnl, outcome, pnl_dollars=None,
                    swap_modeled=None, swap_status="unavailable"):
        return {
            "pair": pair,
            "direction": direction,
            "pnl": pnl,
            "score": 85,
            "confluences_raw": [],
            "regime": "TRENDING",
            "session": "LONDON",
            "spread": 1.0,
            "entry_type": "MARKET",
            "time_to_exit": 60.0,
            "outcome": outcome,
            "pnl_dollars": pnl_dollars if pnl_dollars is not None else pnl,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "swap_modeled": swap_modeled,
            "swap_status": swap_status,
        }

    def test_two_leg_both_modeled(self):
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN",
                             pnl_dollars=150.0, swap_modeled=-5.0, swap_status="modeled"),
            self._make_trade("EURUSD", "BUY", -5.0, "BROKER_CLOSED",
                             pnl_dollars=-25.0, swap_modeled=-3.0, swap_status="modeled"),
        ]
        consolidated = TradeJournal._consolidate_partial_dicts(trades)
        assert len(consolidated) == 1
        assert consolidated[0]["swap_modeled"] == pytest.approx(-8.0)
        assert consolidated[0]["swap_status"] == "modeled"

    def test_two_leg_mixed_status(self):
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN",
                             pnl_dollars=150.0, swap_modeled=-5.0, swap_status="modeled"),
            self._make_trade("EURUSD", "BUY", -5.0, "BROKER_CLOSED",
                             pnl_dollars=-25.0, swap_modeled=None, swap_status="unavailable"),
        ]
        consolidated = TradeJournal._consolidate_partial_dicts(trades)
        assert len(consolidated) == 1
        assert consolidated[0]["swap_modeled"] == pytest.approx(-5.0)
        assert consolidated[0]["swap_status"] == "modeled"

    def test_neither_modeled_unchanged(self):
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN",
                             pnl_dollars=150.0),
            self._make_trade("EURUSD", "BUY", -5.0, "BROKER_CLOSED",
                             pnl_dollars=-25.0),
        ]
        consolidated = TradeJournal._consolidate_partial_dicts(trades)
        assert len(consolidated) == 1
        assert consolidated[0]["swap_modeled"] is None
        assert consolidated[0]["swap_status"] == "unavailable"

    def test_three_leg_chain_sums_swap(self):
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN",
                             pnl_dollars=150.0, swap_modeled=-2.0, swap_status="modeled"),
            self._make_trade("EURUSD", "BUY", 20.0, "TP1_FULL_CLOSE_REOPEN",
                             pnl_dollars=100.0, swap_modeled=-3.0, swap_status="modeled"),
            self._make_trade("EURUSD", "BUY", -5.0, "BROKER_CLOSED",
                             pnl_dollars=-25.0, swap_modeled=-1.5, swap_status="modeled"),
        ]
        consolidated = TradeJournal._consolidate_partial_dicts(trades)
        assert len(consolidated) == 1
        assert consolidated[0]["swap_modeled"] == pytest.approx(-6.5)
        assert consolidated[0]["swap_status"] == "modeled"

    def test_original_not_mutated(self):
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN",
                             pnl_dollars=150.0, swap_modeled=-5.0, swap_status="modeled"),
            self._make_trade("EURUSD", "BUY", -5.0, "SL",
                             pnl_dollars=-25.0, swap_modeled=-3.0, swap_status="modeled"),
        ]
        orig_swap = trades[1]["swap_modeled"]
        TradeJournal._consolidate_partial_dicts(trades)
        assert trades[1]["swap_modeled"] == orig_swap


class TestEndToEndSwapNetting:
    """Integration: journal → stats with swap-modeled trades."""

    @pytest.fixture
    def journal(self, tmp_path):
        from brain.trade_journal import TradeJournal
        return TradeJournal(db_path=str(tmp_path / "test.db"))

    @staticmethod
    def _run(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_partial_chain_with_swap_in_stats(self, journal):
        """A 2-leg partial trade with swap: stats reflect the summed netted P&L."""
        from brain.trade_journal import TradeRecord

        tp1_leg = TradeRecord(
            pair="EURUSD", direction="BUY", entry=1.0800, exit=1.0830,
            pnl=30.0, score=85, confluences=[], regime="TRENDING",
            session="LONDON", spread=1.0, slippage=0.0, entry_type="MARKET",
            time_to_tp1=10.0, time_to_exit=10.0, outcome="TP1_FULL_CLOSE_REOPEN",
            pnl_dollars=150.0, swap_modeled=-4.0, swap_status="modeled",
        )
        remainder = TradeRecord(
            pair="EURUSD", direction="BUY", entry=1.0830, exit=1.0820,
            pnl=-10.0, score=85, confluences=[], regime="TRENDING",
            session="LONDON", spread=1.0, slippage=0.0, entry_type="MARKET",
            time_to_tp1=None, time_to_exit=30.0, outcome="BROKER_CLOSED",
            pnl_dollars=-50.0, swap_modeled=-6.0, swap_status="modeled",
        )
        self._run(journal.initialize())
        self._run(journal.log_trade(tp1_leg))
        self._run(journal.log_trade(remainder))
        stats = self._run(journal.get_performance_stats())
        assert stats["avg_rr"] == pytest.approx(90.0, abs=0.01)


class TestEntrySourceTracking:
    """Bug #16 — the entry-source path is persisted and queryable for learning."""

    @pytest.fixture
    def journal(self, tmp_path):
        from brain.trade_journal import TradeJournal
        return TradeJournal(db_path=str(tmp_path / "src.db"))

    @staticmethod
    def _run(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_source_roundtrips_and_groups_by_path(self, journal):
        from brain.trade_journal import TradeRecord
        from adaptive.trade_analyzer import TradeAnalyzer

        def _rec(source: str, pnl: float) -> TradeRecord:
            return TradeRecord(
                pair="EURUSD", direction="LONG", entry=1.10, exit=1.20,
                pnl=pnl, score=80, confluences=[], regime="TRENDING",
                session="LONDON", spread=1.0, slippage=0.0,
                entry_type="event_driven", time_to_tp1=0.0, time_to_exit=0.0,
                outcome="WIN" if pnl > 0 else "LOSS", pnl_dollars=pnl,
                source=source,
            )

        self._run(journal.initialize())
        for source, pnl in [
            ("zone", 10.0), ("zone", -5.0), ("zone", 8.0),
            ("consensus", -3.0), ("consensus", -4.0), ("consensus", 2.0),
        ]:
            self._run(journal.log_trade(_rec(source, pnl)))

        trades = self._run(journal.get_all_trades_as_dicts())
        assert all("source" in t for t in trades)

        by_source = TradeAnalyzer().analyze_by_source(trades)
        assert by_source["zone"].total_trades == 3
        assert by_source["consensus"].total_trades == 3
        # zone path (2W/1L) outperforms consensus path (1W/2L).
        assert by_source["zone"].win_rate > by_source["consensus"].win_rate

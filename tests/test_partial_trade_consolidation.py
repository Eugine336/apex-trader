"""
APEX TRADER — Partial-Trade Consolidation Tests

Validates that TP1_FULL_CLOSE_REOPEN trades (Deriv full-close + reopen at TP1)
are merged with their continuation trade so each logical trade contributes
exactly once to win-rate / expectancy / profit-factor accounting.
"""

import asyncio
from datetime import datetime, timezone

import pytest


class TestConsolidatePartialRows:
    """Tests for the tuple-based consolidation used by get_performance_stats."""

    @staticmethod
    def _make_row(pair, direction, pnl, outcome, pnl_dollars=None, session="LONDON",
                  time_to_exit=60.0, swap_modeled=None, swap_status="unavailable"):
        return (
            pair,            # 0
            session,         # 1
            pnl,             # 2
            time_to_exit,    # 3
            outcome,         # 4
            pnl_dollars if pnl_dollars is not None else pnl,  # 5
            swap_modeled,    # 6
            swap_status,     # 7
            direction,       # 8
        )

    def test_partial_then_final_counts_as_one(self):
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_row("EURUSD", "BUY", -5.0, "BROKER_CLOSED", pnl_dollars=-25.0),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 1
        merged = consolidated[0]
        assert merged[2] == pytest.approx(25.0)
        assert merged[5] == pytest.approx(125.0)
        assert merged[4] == "BROKER_CLOSED"

    def test_no_partial_unchanged(self):
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP2", pnl_dollars=150.0),
            self._make_row("GBPUSD", "SELL", -10.0, "SL", pnl_dollars=-50.0),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 2
        assert consolidated[0][2] == 30.0
        assert consolidated[1][2] == -10.0

    def test_orphaned_partial_kept(self):
        """A TP1_FULL_CLOSE_REOPEN with no continuation is kept as-is."""
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_row("GBPUSD", "SELL", -10.0, "SL", pnl_dollars=-50.0),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 2

    def test_direction_mismatch_not_merged(self):
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_row("EURUSD", "SELL", -5.0, "SL", pnl_dollars=-25.0),
            self._make_row("EURUSD", "BUY", 10.0, "TP2", pnl_dollars=50.0),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 2
        merged = consolidated[1]
        assert merged[2] == pytest.approx(40.0)
        assert merged[4] == "TP2"

    def test_empty_rows(self):
        from brain.trade_journal import TradeJournal

        assert TradeJournal._consolidate_partial_rows([]) == []

    def test_multiple_pairs_independent(self):
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_row("GBPUSD", "BUY", 20.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=100.0),
            self._make_row("EURUSD", "BUY", -5.0, "SL", pnl_dollars=-25.0),
            self._make_row("GBPUSD", "BUY", 15.0, "TP2", pnl_dollars=75.0),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 2
        eur = [r for r in consolidated if r[0] == "EURUSD"][0]
        gbp = [r for r in consolidated if r[0] == "GBPUSD"][0]
        assert eur[2] == pytest.approx(25.0)
        assert eur[5] == pytest.approx(125.0)
        assert gbp[2] == pytest.approx(35.0)
        assert gbp[5] == pytest.approx(175.0)

    def test_three_leg_chain_sums_all_pnl(self):
        """marker→marker→final: all three legs' P&L must appear in the survivor."""
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_row("EURUSD", "BUY", 20.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=100.0),
            self._make_row("EURUSD", "BUY", -5.0, "BROKER_CLOSED", pnl_dollars=-25.0),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 1
        merged = consolidated[0]
        assert merged[2] == pytest.approx(45.0)
        assert merged[5] == pytest.approx(225.0)
        assert merged[4] == "BROKER_CLOSED"

    def test_four_leg_chain_sums_all_pnl(self):
        """marker→marker→marker→final: four legs collapse to one."""
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_row("EURUSD", "BUY", 20.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=100.0),
            self._make_row("EURUSD", "BUY", 10.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=50.0),
            self._make_row("EURUSD", "BUY", -5.0, "TP2", pnl_dollars=-25.0),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 1
        merged = consolidated[0]
        assert merged[2] == pytest.approx(55.0)
        assert merged[5] == pytest.approx(275.0)
        assert merged[4] == "TP2"

    def test_orphaned_marker_chain_collapses_to_one(self):
        """marker→marker with no final: collapse to one row summing both."""
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_row("EURUSD", "BUY", 20.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=100.0),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 1
        merged = consolidated[0]
        assert merged[2] == pytest.approx(50.0)
        assert merged[5] == pytest.approx(250.0)

    def test_interleaved_chains_independent(self):
        """Two interleaved 3-leg chains for different pairs stay independent."""
        from brain.trade_journal import TradeJournal

        rows = [
            self._make_row("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_row("GBPUSD", "SELL", 25.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=120.0),
            self._make_row("EURUSD", "BUY", 10.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=50.0),
            self._make_row("GBPUSD", "SELL", -8.0, "SL", pnl_dollars=-40.0),
            self._make_row("EURUSD", "BUY", -5.0, "BROKER_CLOSED", pnl_dollars=-25.0),
        ]
        consolidated = TradeJournal._consolidate_partial_rows(rows)
        assert len(consolidated) == 2
        eur = [r for r in consolidated if r[0] == "EURUSD"][0]
        gbp = [r for r in consolidated if r[0] == "GBPUSD"][0]
        assert eur[2] == pytest.approx(35.0)
        assert eur[5] == pytest.approx(175.0)
        assert eur[4] == "BROKER_CLOSED"
        assert gbp[2] == pytest.approx(17.0)
        assert gbp[5] == pytest.approx(80.0)
        assert gbp[4] == "SL"


class TestConsolidatePartialDicts:
    """Tests for the dict-based consolidation used by get_all_trades_as_dicts."""

    @staticmethod
    def _make_trade(pair, direction, pnl, outcome, pnl_dollars=None, score=85,
                    regime="TRENDING", session="LONDON"):
        return {
            "pair": pair,
            "direction": direction,
            "pnl": pnl,
            "score": score,
            "confluences_raw": [],
            "regime": regime,
            "session": session,
            "spread": 1.0,
            "entry_type": "MARKET",
            "time_to_exit": 60.0,
            "outcome": outcome,
            "pnl_dollars": pnl_dollars if pnl_dollars is not None else pnl,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "swap_modeled": None,
            "swap_status": "unavailable",
        }

    def test_partial_then_final_counts_as_one(self):
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_trade("EURUSD", "BUY", -5.0, "BROKER_CLOSED", pnl_dollars=-25.0),
        ]
        consolidated = TradeJournal._consolidate_partial_dicts(trades)
        assert len(consolidated) == 1
        assert consolidated[0]["pnl"] == pytest.approx(25.0)
        assert consolidated[0]["pnl_dollars"] == pytest.approx(125.0)
        assert consolidated[0]["outcome"] == "BROKER_CLOSED"

    def test_no_partial_unchanged(self):
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP2", pnl_dollars=150.0),
            self._make_trade("GBPUSD", "SELL", -10.0, "SL", pnl_dollars=-50.0),
        ]
        consolidated = TradeJournal._consolidate_partial_dicts(trades)
        assert len(consolidated) == 2

    def test_original_not_mutated(self):
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_trade("EURUSD", "BUY", -5.0, "SL", pnl_dollars=-25.0),
        ]
        orig_pnl = trades[1]["pnl"]
        TradeJournal._consolidate_partial_dicts(trades)
        assert trades[1]["pnl"] == orig_pnl

    def test_empty_trades(self):
        from brain.trade_journal import TradeJournal

        assert TradeJournal._consolidate_partial_dicts([]) == []

    def test_three_leg_chain_sums_all_pnl(self):
        """marker→marker→final: all three legs' P&L must appear in the survivor."""
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_trade("EURUSD", "BUY", 20.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=100.0),
            self._make_trade("EURUSD", "BUY", -5.0, "BROKER_CLOSED", pnl_dollars=-25.0),
        ]
        consolidated = TradeJournal._consolidate_partial_dicts(trades)
        assert len(consolidated) == 1
        assert consolidated[0]["pnl"] == pytest.approx(45.0)
        assert consolidated[0]["pnl_dollars"] == pytest.approx(225.0)
        assert consolidated[0]["outcome"] == "BROKER_CLOSED"

    def test_four_leg_chain_sums_all_pnl(self):
        """marker→marker→marker→final: four legs collapse to one."""
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_trade("EURUSD", "BUY", 20.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=100.0),
            self._make_trade("EURUSD", "BUY", 10.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=50.0),
            self._make_trade("EURUSD", "BUY", -5.0, "TP2", pnl_dollars=-25.0),
        ]
        consolidated = TradeJournal._consolidate_partial_dicts(trades)
        assert len(consolidated) == 1
        assert consolidated[0]["pnl"] == pytest.approx(55.0)
        assert consolidated[0]["pnl_dollars"] == pytest.approx(275.0)
        assert consolidated[0]["outcome"] == "TP2"

    def test_orphaned_marker_chain_collapses_to_one(self):
        """marker→marker with no final: collapse to one row summing both."""
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_trade("EURUSD", "BUY", 20.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=100.0),
        ]
        consolidated = TradeJournal._consolidate_partial_dicts(trades)
        assert len(consolidated) == 1
        assert consolidated[0]["pnl"] == pytest.approx(50.0)
        assert consolidated[0]["pnl_dollars"] == pytest.approx(250.0)

    def test_interleaved_chains_independent(self):
        """Two interleaved 3-leg chains for different pairs stay independent."""
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_trade("GBPUSD", "SELL", 25.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=120.0),
            self._make_trade("EURUSD", "BUY", 10.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=50.0),
            self._make_trade("GBPUSD", "SELL", -8.0, "SL", pnl_dollars=-40.0),
            self._make_trade("EURUSD", "BUY", -5.0, "BROKER_CLOSED", pnl_dollars=-25.0),
        ]
        consolidated = TradeJournal._consolidate_partial_dicts(trades)
        assert len(consolidated) == 2
        eur = [t for t in consolidated if t["pair"] == "EURUSD"][0]
        gbp = [t for t in consolidated if t["pair"] == "GBPUSD"][0]
        assert eur["pnl"] == pytest.approx(35.0)
        assert eur["pnl_dollars"] == pytest.approx(175.0)
        assert eur["outcome"] == "BROKER_CLOSED"
        assert gbp["pnl"] == pytest.approx(17.0)
        assert gbp["pnl_dollars"] == pytest.approx(80.0)
        assert gbp["outcome"] == "SL"

    def test_original_not_mutated_chain(self):
        """Chained consolidation must not mutate the caller's input dicts."""
        from brain.trade_journal import TradeJournal

        trades = [
            self._make_trade("EURUSD", "BUY", 30.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=150.0),
            self._make_trade("EURUSD", "BUY", 20.0, "TP1_FULL_CLOSE_REOPEN", pnl_dollars=100.0),
            self._make_trade("EURUSD", "BUY", -5.0, "SL", pnl_dollars=-25.0),
        ]
        orig_pnl_0 = trades[0]["pnl"]
        orig_pnl_1 = trades[1]["pnl"]
        orig_pnl_2 = trades[2]["pnl"]
        TradeJournal._consolidate_partial_dicts(trades)
        assert trades[0]["pnl"] == orig_pnl_0
        assert trades[1]["pnl"] == orig_pnl_1
        assert trades[2]["pnl"] == orig_pnl_2


class TestConsolidationEndToEnd:
    """Integration test: journal → stats with a partial-then-final trade."""

    @pytest.fixture
    def journal(self, tmp_path):
        from brain.trade_journal import TradeJournal

        db = tmp_path / "test_journal.db"
        return TradeJournal(db_path=str(db))

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_partial_trade_single_outcome_in_stats(self, journal):
        from brain.trade_journal import TradeRecord

        tp1_leg = TradeRecord(
            pair="EURUSD", direction="BUY", entry=1.0800, exit=1.0830,
            pnl=30.0, score=85, confluences=[], regime="TRENDING",
            session="LONDON", spread=1.0, slippage=0.0, entry_type="MARKET",
            time_to_tp1=10.0, time_to_exit=10.0, outcome="TP1_FULL_CLOSE_REOPEN",
            pnl_dollars=150.0,
        )
        remainder_leg = TradeRecord(
            pair="EURUSD", direction="BUY", entry=1.0830, exit=1.0820,
            pnl=-10.0, score=85, confluences=[], regime="TRENDING",
            session="LONDON", spread=1.0, slippage=0.0, entry_type="MARKET",
            time_to_tp1=None, time_to_exit=30.0, outcome="BROKER_CLOSED",
            pnl_dollars=-50.0,
        )
        normal_trade = TradeRecord(
            pair="GBPUSD", direction="SELL", entry=1.2500, exit=1.2480,
            pnl=20.0, score=80, confluences=[], regime="RANGING",
            session="NY", spread=1.5, slippage=0.0, entry_type="MARKET",
            time_to_tp1=5.0, time_to_exit=20.0, outcome="TP2",
            pnl_dollars=100.0,
        )

        self._run(journal.initialize())
        self._run(journal.log_trade(tp1_leg))
        self._run(journal.log_trade(remainder_leg))
        self._run(journal.log_trade(normal_trade))

        stats = self._run(journal.get_performance_stats())
        assert stats["win_rate"] == pytest.approx(100.0)

        trades = self._run(journal.get_all_trades_as_dicts())
        assert len(trades) == 2

        eur = [t for t in trades if t["pair"] == "EURUSD"][0]
        assert eur["pnl"] == pytest.approx(20.0)
        assert eur["pnl_dollars"] == pytest.approx(100.0)
        assert eur["outcome"] == "BROKER_CLOSED"

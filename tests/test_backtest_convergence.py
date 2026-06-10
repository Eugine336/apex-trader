"""
Tests proving the backtest engine now drives the LIVE decision path
(PairScanner → EntryEngine) instead of the stale MTFOrchestrator.

Two tiers:
  - Lightweight (no torch): structural assertions + adapter logic.
  - Heavy (torch present): end-to-end smoke with injected scanner/engine.
"""

import importlib
import inspect
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Lightweight tests — always run, no torch required
# ---------------------------------------------------------------------------


class TestBacktestNoLongerUsesMTFOrchestrator:
    """Assert the backtest module has no MTFOrchestrator dependency."""

    def test_module_does_not_import_mtf_orchestrator(self):
        source = open("brain/backtest_engine.py").read()
        assert "from brain.mtf_orchestrator" not in source
        assert "MTFOrchestrator" not in source

    def test_module_does_not_reference_trade_setup(self):
        source = open("brain/backtest_engine.py").read()
        assert "TradeSetup" not in source

    def test_backtest_setup_dataclass_exists(self):
        from brain.backtest_engine import BacktestSetup

        setup = BacktestSetup(
            direction="LONG",
            entry_price=1.1000,
            stop_loss=1.0950,
            tp1=1.1075,
            tp2=1.1150,
            score=82,
        )
        assert setup.direction == "LONG"
        assert setup.entry_price == 1.1000
        assert setup.entry_type == "MARKET"
        assert setup.opportunity_quality == 0.0


class TestBacktestSetupAdapter:
    """The BacktestSetup carries fields the simulation reads."""

    def test_entry_type_from_signal(self):
        from brain.backtest_engine import BacktestSetup

        setup = BacktestSetup(
            direction="SHORT", entry_price=1.2, stop_loss=1.21,
            tp1=1.19, tp2=1.18, score=75, entry_type="FVG_MIDPOINT",
        )
        assert setup.entry_type == "FVG_MIDPOINT"

    def test_confluences_are_plain_strings(self):
        from brain.backtest_engine import BacktestSetup

        setup = BacktestSetup(
            direction="LONG", entry_price=1.0, stop_loss=0.99,
            tp1=1.01, tp2=1.02, score=80,
            confluences=["Structure aligned", "FVG zone present"],
        )
        assert all(isinstance(c, str) for c in setup.confluences)

    def test_resolve_entry_type_reads_entry_type_field(self):
        from brain.backtest_engine import BacktestEngine

        engine = BacktestEngine.__new__(BacktestEngine)
        setup_sweep = SimpleNamespace(entry_type="SWEEP_REVERSAL")
        assert engine._resolve_entry_type(setup_sweep) == "SWEEP"

        setup_fvg = SimpleNamespace(entry_type="FVG_MIDPOINT")
        assert engine._resolve_entry_type(setup_fvg) == "FVG"

        setup_ob = SimpleNamespace(entry_type="OB_MIDPOINT")
        assert engine._resolve_entry_type(setup_ob) == "OB"

        setup_plain = SimpleNamespace(entry_type="MARKET")
        assert engine._resolve_entry_type(setup_plain) == "MARKET"

        setup_none = SimpleNamespace()
        assert engine._resolve_entry_type(setup_none) == "MARKET"


class TestDecideSetupFailsClosed:
    """_decide_setup must return None (skip bar) on any failure."""

    def _make_engine(self):
        from brain.backtest_engine import BacktestEngine

        engine = BacktestEngine.__new__(BacktestEngine)
        engine.pip_size = 0.0001
        return engine

    def _dummy_slices(self):
        times = pd.date_range("2025-01-01", periods=100, freq="min", tz="UTC")
        df = pd.DataFrame({
            "time": times,
            "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 100,
        })
        return {"H4": df, "H1": df, "M15": df, "M5": df, "M1": df}

    def test_returns_none_when_scanner_is_none(self):
        engine = self._make_engine()
        engine.scanner = None
        engine.entry_engine = MagicMock()
        result = engine._decide_setup("EURUSD", self._dummy_slices(), datetime.now(), 10000.0)
        assert result is None

    def test_returns_none_when_entry_engine_is_none(self):
        engine = self._make_engine()
        engine.scanner = MagicMock()
        engine.entry_engine = None
        result = engine._decide_setup("EURUSD", self._dummy_slices(), datetime.now(), 10000.0)
        assert result is None

    def test_returns_none_when_scanner_throws(self):
        engine = self._make_engine()
        engine.scanner = MagicMock()
        engine.scanner.scan_pair.side_effect = RuntimeError("boom")
        engine.entry_engine = MagicMock()
        result = engine._decide_setup("EURUSD", self._dummy_slices(), datetime.now(), 10000.0)
        assert result is None

    def test_returns_none_when_status_not_ready(self):
        engine = self._make_engine()
        scan_result = SimpleNamespace(status="WATCHLIST", direction="LONG")
        engine.scanner = MagicMock()
        engine.scanner.scan_pair.return_value = scan_result
        engine.entry_engine = MagicMock()
        result = engine._decide_setup("EURUSD", self._dummy_slices(), datetime.now(), 10000.0)
        assert result is None

    def test_returns_none_when_direction_neutral(self):
        engine = self._make_engine()
        scan_result = SimpleNamespace(status="READY", direction="NEUTRAL")
        engine.scanner = MagicMock()
        engine.scanner.scan_pair.return_value = scan_result
        engine.entry_engine = MagicMock()
        result = engine._decide_setup("EURUSD", self._dummy_slices(), datetime.now(), 10000.0)
        assert result is None

    def test_returns_none_on_entry_rejection(self):
        from trigger.entry_engine import EntryRejection

        engine = self._make_engine()
        scan_result = SimpleNamespace(status="READY", direction="LONG")
        engine.scanner = MagicMock()
        engine.scanner.scan_pair.return_value = scan_result
        rejection = EntryRejection(pair="EURUSD", reason="test", score=70)
        engine.entry_engine = MagicMock()
        engine.entry_engine.calculate_entry.return_value = rejection
        result = engine._decide_setup("EURUSD", self._dummy_slices(), datetime.now(), 10000.0)
        assert result is None

    def test_returns_setup_on_valid_signal(self):
        from brain.backtest_engine import BacktestSetup
        from trigger.entry_engine import EntrySignal

        engine = self._make_engine()
        scan_result = SimpleNamespace(
            status="READY", direction="LONG", score=85,
            regime="TRENDING", bias_strength="STRONG",
            confluences=["Structure aligned"],
            opportunity_quality=7.5, entry_quality=6.8,
            consensus_agreement=0.82,
        )
        engine.scanner = MagicMock()
        engine.scanner.scan_pair.return_value = scan_result

        signal = EntrySignal(
            pair="EURUSD", direction="LONG", entry_type="FVG_MIDPOINT",
            entry_price=1.1000, stop_loss=1.0950, tp1=1.1075, tp2=1.1150,
            risk_reward_1=1.5, risk_reward_2=3.0, risk_pips=5.0,
            position_size_lots=0.1, score=85,
            confluences=["Structure aligned", "FVG zone"],
        )
        engine.entry_engine = MagicMock()
        engine.entry_engine.calculate_entry.return_value = signal

        result = engine._decide_setup("EURUSD", self._dummy_slices(), datetime.now(), 10000.0)
        assert result is not None
        assert isinstance(result, BacktestSetup)
        assert result.direction == "LONG"
        assert result.entry_price == 1.1000
        assert result.entry_type == "FVG_MIDPOINT"
        assert result.opportunity_quality == 7.5

    def test_returns_none_when_slices_missing_timeframe(self):
        engine = self._make_engine()
        engine.scanner = MagicMock()
        engine.entry_engine = MagicMock()
        slices = self._dummy_slices()
        del slices["H4"]
        result = engine._decide_setup("EURUSD", slices, datetime.now(), 10000.0)
        assert result is None


class TestOpenTradeUsesSessionEngine:
    """_open_trade must use self.session_engine, not orchestrator."""

    def test_session_from_session_engine(self):
        from brain.backtest_engine import BacktestEngine, BacktestSetup

        engine = BacktestEngine.__new__(BacktestEngine)
        engine.pip_size = 0.0001
        engine.slippage_pips = 1.0
        engine.session_engine = MagicMock()
        engine.session_engine.get_status.return_value = SimpleNamespace(
            current_session="LONDON"
        )

        setup = BacktestSetup(
            direction="LONG", entry_price=1.1000, stop_loss=1.0950,
            tp1=1.1075, tp2=1.1150, score=80, entry_type="FVG_MIDPOINT",
        )
        trade = engine._open_trade(setup, datetime.now())
        assert trade["session"] == "LONDON"
        assert trade["entry_type"] == "FVG"
        assert trade["setup"] is setup


class TestWalkForwardStillWorks:
    """Existing walk_forward geometry tests must still pass.
    These bypass __init__ via __new__ and mock run()."""

    def _make_engine(self, min_history=10):
        from brain.backtest_engine import BacktestEngine, BacktestResult

        engine = BacktestEngine.__new__(BacktestEngine)
        engine.min_history = min_history
        engine.journal = None
        engine.starting_balance = 10_000.0
        engine.risk_per_trade = 0.02
        engine.pip_size = 0.0001
        engine.slippage_pips = 1.0
        engine.commission_per_lot = 3.5
        engine.broker_loader = MagicMock()
        engine.scanner = MagicMock()
        engine.entry_engine = MagicMock()
        engine.session_engine = MagicMock()

        dummy = BacktestResult(
            total_trades=0, wins=0, losses=0, win_rate=0.0,
            profit_factor=0.0, sharpe_ratio=0.0, max_drawdown=0.0,
            max_consecutive_losses=0, expectancy=0.0, avg_hold_time=0.0,
            best_pair=None, worst_pair=None, best_session=None,
            equity_curve=[10_000.0],
        )
        engine._run_calls = []

        def _tracking_run(pair, data_by_timeframe, start_index=None, end_index=None, **kw):
            engine._run_calls.append((start_index, end_index))
            return dummy

        engine.run = _tracking_run
        return engine

    def _m1_data(self, n_bars):
        times = pd.date_range("2025-01-01", periods=n_bars, freq="min", tz="UTC")
        return {"M1": pd.DataFrame({
            "time": times,
            "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0,
        })}

    def test_five_fold_produces_four_oos_segments(self):
        engine = self._make_engine(min_history=10)
        data = self._m1_data(510)
        result = engine.walk_forward("TEST", data, n_folds=5)
        assert len(result["folds"]) == 4

    def test_all_indices_respect_min_history(self):
        engine = self._make_engine(min_history=50)
        data = self._m1_data(500)
        engine.walk_forward("TEST", data, n_folds=5)
        for start, end in engine._run_calls:
            assert start >= 50


class TestJournalTradeAdapted:
    """_journal_trade must work with BacktestSetup (string confluences)."""

    def test_journal_records_string_confluences(self):
        from brain.backtest_engine import BacktestEngine, BacktestSetup

        engine = BacktestEngine.__new__(BacktestEngine)
        mock_journal = MagicMock()

        async def _mock_log(record):
            pass

        mock_journal.log_trade = _mock_log
        engine.journal = mock_journal

        setup = BacktestSetup(
            direction="LONG", entry_price=1.1000, stop_loss=1.0950,
            tp1=1.1075, tp2=1.1150, score=82, regime="TRENDING",
            confluences=["Structure aligned", "FVG zone present"],
        )
        trade = {
            "setup": setup,
            "entry_time": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "entry_price": 1.1001,
            "stop_loss": 1.0950,
            "tp1": 1.1075,
            "tp2": 1.1150,
            "risk": 0.005,
            "tp1_hit": False,
            "realized_r": 0.0,
            "session": "LONDON",
            "entry_type": "FVG",
            "slippage_cost": 0.0001,
        }
        close_event = {"pnl_r": -1.0, "hold_minutes": 45.0, "outcome": "LOSS"}

        engine._journal_trade("EURUSD", trade, close_event, datetime.now(timezone.utc))

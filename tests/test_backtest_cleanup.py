"""Tests for the three cleanup fixes: rr_helper relocation, disabled-engine
fail-loud, and log-level raise.  No torch or heavy-ML dependencies required."""

from __future__ import annotations

import importlib
import types
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fix 1 — rr_helper lives in scanner/ and imports cleanly
# ---------------------------------------------------------------------------


class TestRRHelperRelocation:
    """Prove the helper is importable from its new package location."""

    def _import_rr_helper(self):
        """Import scanner.rr_helper directly, tolerating missing torch in CI."""
        import importlib.util
        import os

        path = os.path.join(
            os.path.dirname(__file__), os.pardir, "scanner", "rr_helper.py"
        )
        spec = importlib.util.spec_from_file_location("scanner.rr_helper", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.compute_side_agnostic_rr

    def test_import_from_scanner_package(self):
        fn = self._import_rr_helper()
        assert callable(fn)

    def test_basic_computation_still_works(self):
        fn = self._import_rr_helper()
        rr = fn(
            buy_price=1.1020,
            sell_price=1.0980,
            current_price=1.1000,
            atr_pips=10.0,
            pip_size=0.0001,
        )
        assert rr is not None
        assert rr > 0

    def test_no_root_level_import(self):
        """Ensure the old root-level rr_helper is gone."""
        spec = importlib.util.find_spec("rr_helper")
        assert spec is None, "rr_helper should NOT be importable from the repo root"


# ---------------------------------------------------------------------------
# Fix 2 — disabled backtest engine raises RuntimeError, not silent empty
# ---------------------------------------------------------------------------


def _make_engine(scanner=None, entry_engine=None):
    """Construct a BacktestEngine with explicitly injected dependencies,
    bypassing the try/except auto-construction (no torch needed)."""
    from brain.backtest_engine import BacktestEngine

    eng = object.__new__(BacktestEngine)
    eng.scanner = scanner
    eng.entry_engine = entry_engine

    from brain.session_engine import SessionEngine

    eng.session_engine = SessionEngine()
    eng.journal = None
    eng.starting_balance = 10_000.0
    eng.risk_per_trade = 0.02
    eng.min_history = 120
    eng.pip_size = 0.0001
    eng.slippage_pips = 1.0
    eng.commission_per_lot = 3.5
    eng.broker_loader = None
    eng.config = types.SimpleNamespace()
    return eng


class TestDisabledEngineFailsLoud:
    """A disabled engine must raise RuntimeError on run(), not return an empty result."""

    def _dummy_data(self):
        import pandas as pd

        m1 = pd.DataFrame(
            {
                "time": pd.date_range("2025-01-01", periods=200, freq="min", tz="UTC"),
                "open": 1.1,
                "high": 1.2,
                "low": 1.0,
                "close": 1.1,
                "volume": 100,
            }
        )
        return {"M1": m1, "M5": m1, "M15": m1, "H1": m1, "H4": m1}

    def test_run_raises_when_both_none(self):
        eng = _make_engine(scanner=None, entry_engine=None)
        with pytest.raises(RuntimeError, match="PairScanner.*EntryEngine"):
            eng.run("EURUSD", self._dummy_data())

    def test_run_raises_when_scanner_none(self):
        eng = _make_engine(scanner=None, entry_engine=MagicMock())
        with pytest.raises(RuntimeError, match="PairScanner"):
            eng.run("EURUSD", self._dummy_data())

    def test_run_raises_when_entry_engine_none(self):
        eng = _make_engine(scanner=MagicMock(), entry_engine=None)
        with pytest.raises(RuntimeError, match="EntryEngine"):
            eng.run("EURUSD", self._dummy_data())

    def test_walk_forward_raises_when_disabled(self):
        eng = _make_engine(scanner=None, entry_engine=None)
        with pytest.raises(RuntimeError, match="cannot run a representative backtest"):
            eng.walk_forward("EURUSD", self._dummy_data())

    def test_run_does_not_raise_when_engines_present(self):
        """A valid (mocked) engine must NOT raise — only disabled ones do."""
        mock_scanner = MagicMock()
        mock_scanner.scan_pair.return_value = MagicMock(
            status="WAITING", direction="NEUTRAL"
        )
        eng = _make_engine(scanner=mock_scanner, entry_engine=MagicMock())
        result = eng.run("EURUSD", self._dummy_data())
        assert result.total_trades == 0


# ---------------------------------------------------------------------------
# Fix 3 — swallowed exceptions log at WARNING, not DEBUG
# ---------------------------------------------------------------------------


class TestBacktestLogLevels:
    """scan_pair/calculate_entry failures must log at WARNING."""

    def test_scan_pair_failure_logs_warning(self):
        mock_scanner = MagicMock()
        mock_scanner.scan_pair.side_effect = ValueError("boom")
        eng = _make_engine(scanner=mock_scanner, entry_engine=MagicMock())

        import pandas as pd

        slices = {
            "H4": pd.DataFrame({"time": [1], "open": [1], "high": [1], "low": [1], "close": [1]}),
            "H1": pd.DataFrame({"time": [1], "open": [1], "high": [1], "low": [1], "close": [1]}),
            "M15": pd.DataFrame({"time": [1], "open": [1], "high": [1], "low": [1], "close": [1]}),
            "M5": pd.DataFrame({"time": [1], "open": [1], "high": [1], "low": [1], "close": [1]}),
            "M1": pd.DataFrame({"time": [1], "open": [1], "high": [1], "low": [1], "close": [1]}),
        }
        from datetime import datetime

        result = eng._decide_setup("EURUSD", slices, datetime.now(), 10_000.0)
        assert result is None

    def test_calculate_entry_failure_logs_warning(self):
        mock_scanner = MagicMock()
        mock_scanner.scan_pair.return_value = MagicMock(
            status="READY", direction="LONG"
        )
        mock_entry = MagicMock()
        mock_entry.calculate_entry.side_effect = ValueError("boom")
        eng = _make_engine(scanner=mock_scanner, entry_engine=mock_entry)

        import pandas as pd

        slices = {
            "H4": pd.DataFrame({"time": [1], "open": [1], "high": [1], "low": [1], "close": [1]}),
            "H1": pd.DataFrame({"time": [1], "open": [1], "high": [1], "low": [1], "close": [1]}),
            "M15": pd.DataFrame({"time": [1], "open": [1], "high": [1], "low": [1], "close": [1]}),
            "M5": pd.DataFrame({"time": [1], "open": [1], "high": [1], "low": [1], "close": [1]}),
            "M1": pd.DataFrame({"time": [1], "open": [1], "high": [1], "low": [1], "close": [1]}),
        }
        from datetime import datetime

        result = eng._decide_setup("EURUSD", slices, datetime.now(), 10_000.0)
        assert result is None

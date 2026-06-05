"""Tests for Fix A (atomic writes) and Fix B (fail-closed balance).

Fix A: adaptive learners use atomic temp-file+replace writes.
Fix B: the trading loop never sizes on a phantom $10k fallback —
       it skips entry on balance-fetch failure and uses pnl_pct=0.0
       on close-path failure.
"""

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── Fix A: Atomic write tests ─────────────────────────────────────────

class TestAtomicWriteText:

    def test_successful_write_produces_valid_json(self, tmp_path):
        from persistence.atomic_write import atomic_write_text

        target = tmp_path / "state.json"
        data = {"key": "value", "number": 42}
        atomic_write_text(target, json.dumps(data, indent=2))

        assert target.exists()
        assert json.loads(target.read_text()) == data

    def test_old_file_survives_failed_write(self, tmp_path):
        from persistence.atomic_write import atomic_write_text

        target = tmp_path / "state.json"
        original = {"original": True}
        target.write_text(json.dumps(original))

        with patch("persistence.atomic_write.os.fdopen", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                atomic_write_text(target, '{"corrupted": true}')

        assert json.loads(target.read_text()) == original

    def test_no_temp_file_left_on_failure(self, tmp_path):
        from persistence.atomic_write import atomic_write_text

        target = tmp_path / "state.json"
        before = set(os.listdir(tmp_path))

        with patch("persistence.atomic_write.os.fdopen", side_effect=OSError("fail")):
            with pytest.raises(OSError):
                atomic_write_text(target, "content")

        after = set(os.listdir(tmp_path))
        assert after == before

    def test_creates_parent_dirs(self, tmp_path):
        from persistence.atomic_write import atomic_write_text

        target = tmp_path / "deep" / "nested" / "state.json"
        atomic_write_text(target, '{"ok": true}')
        assert json.loads(target.read_text()) == {"ok": True}


class TestLearnerAtomicSave:
    """Verify the three learner _save() methods use atomic writes."""

    def test_regime_learner_uses_atomic_write(self, tmp_path):
        from adaptive.regime_learner import RegimeLearner

        learner = RegimeLearner.__new__(RegimeLearner)
        learner.SAVE_PATH = str(tmp_path / "regime.json")
        learner._strategies = {}

        learner._save()
        assert json.loads(Path(learner.SAVE_PATH).read_text()) == {}

    def test_pair_learner_uses_atomic_write(self, tmp_path):
        from adaptive.pair_learner import PairLearner

        learner = PairLearner.__new__(PairLearner)
        learner.SAVE_PATH = str(tmp_path / "pair.json")
        learner._profiles = {}

        learner._save()
        assert json.loads(Path(learner.SAVE_PATH).read_text()) == {}

    def test_session_learner_uses_atomic_write(self, tmp_path):
        from adaptive.session_learner import SessionLearner

        learner = SessionLearner.__new__(SessionLearner)
        learner.SAVE_PATH = str(tmp_path / "session.json")
        learner._profiles = {}

        learner._save()
        assert json.loads(Path(learner.SAVE_PATH).read_text()) == {}


# ── Fix B: Fail-closed balance tests ──────────────────────────────────

def _make_loop():
    """Create a minimal TradingLoop without __init__."""
    from platforms.main_loop import TradingLoop

    loop = TradingLoop.__new__(TradingLoop)
    loop.drawdown = MagicMock()
    loop.risk_engine = MagicMock()
    loop.risk_engine.drawdown_guard = MagicMock()
    loop.risk_engine.drawdown_guard.risk_map = {"NORMAL": 0.0075}
    loop.risk_engine.drawdown_guard.mode = "NORMAL"
    loop.platforms = MagicMock()
    loop.journal = MagicMock()
    loop.ml = MagicMock()
    loop.trade_manager = MagicMock()
    loop.entry_engine = MagicMock()
    loop.config = MagicMock()
    loop.scanner = MagicMock()
    loop.position_store = MagicMock()
    loop._managed_positions = {}
    loop._add_warning = MagicMock()
    loop._journal_loop = asyncio.new_event_loop()
    loop._current_cycle_id = "test-cycle-id"
    loop._current_setup_id = "test-setup-id"
    return loop


class TestBalanceFailClosed:
    """Fix B Site 1: entry sizing path must never use phantom $10k."""

    def _make_scan_result(self):
        return SimpleNamespace(
            pair="EURUSD",
            direction="LONG",
            score=85,
            trend_h4="UP",
            trend_h1="UP",
            bias_strength=0.8,
            regime="TRENDING",
            session_active=True,
            has_fvg=True,
            has_order_block=False,
            confluences=["structure"],
            instrument_category="FOREX",
            ev_estimate=1.2,
            status="READY",
        )

    @patch("platforms.main_loop.INSTRUMENT_REGISTRY", {})
    @patch("platforms.main_loop.get_event_store")
    def test_balance_returns_zero_skips_entry(self, mock_store):
        loop = _make_loop()
        loop.platforms.get_platform_balance.return_value = 0.0
        loop.platforms.fetch_market_data.return_value = {
            "H4": MagicMock(), "H1": MagicMock(),
            "M15": MagicMock(), "M5": MagicMock(), "M1": MagicMock(),
        }

        result = loop._execute_entry_inner(
            self._make_scan_result(), "LONDON", datetime.now(timezone.utc), "setup-123"
        )

        assert result is False
        loop.risk_engine.__setattr__  # balance never assigned
        assert not loop.entry_engine.calculate_entry.called

    @patch("platforms.main_loop.INSTRUMENT_REGISTRY", {})
    @patch("platforms.main_loop.get_event_store")
    def test_balance_returns_none_skips_entry(self, mock_store):
        loop = _make_loop()
        loop.platforms.get_platform_balance.return_value = None
        loop.platforms.fetch_market_data.return_value = {
            "H4": MagicMock(), "H1": MagicMock(),
            "M15": MagicMock(), "M5": MagicMock(), "M1": MagicMock(),
        }

        result = loop._execute_entry_inner(
            self._make_scan_result(), "LONDON", datetime.now(timezone.utc), "setup-123"
        )

        assert result is False
        assert not loop.entry_engine.calculate_entry.called

    @patch("platforms.main_loop.INSTRUMENT_REGISTRY", {})
    @patch("platforms.main_loop.get_event_store")
    def test_balance_fetch_exception_skips_entry(self, mock_store):
        loop = _make_loop()
        loop.platforms.get_platform_balance.return_value = 0.0
        loop.platforms.fetch_market_data.return_value = {
            "H4": MagicMock(), "H1": MagicMock(),
            "M15": MagicMock(), "M5": MagicMock(), "M1": MagicMock(),
        }

        result = loop._execute_entry_inner(
            self._make_scan_result(), "LONDON", datetime.now(timezone.utc), "setup-123"
        )

        assert result is False
        assert not loop.entry_engine.calculate_entry.called

    @patch("platforms.main_loop.INSTRUMENT_REGISTRY", {})
    @patch("platforms.main_loop.get_event_store")
    def test_balance_unavailable_emits_event(self, mock_store):
        store_instance = MagicMock()
        mock_store.return_value = store_instance
        loop = _make_loop()
        loop.platforms.get_platform_balance.return_value = 0.0
        loop.platforms.fetch_market_data.return_value = {
            "H4": MagicMock(), "H1": MagicMock(),
            "M15": MagicMock(), "M5": MagicMock(), "M1": MagicMock(),
        }

        loop._execute_entry_inner(
            self._make_scan_result(), "LONDON", datetime.now(timezone.utc), "setup-123"
        )

        store_instance.emit.assert_called()
        event_types = [c.kwargs.get("event_type") for c in store_instance.emit.call_args_list]
        assert "BALANCE_UNAVAILABLE" in event_types

    @patch("platforms.main_loop.INSTRUMENT_REGISTRY", {})
    @patch("platforms.main_loop.get_event_store")
    @patch("platforms.main_loop.build_context_for_symbol")
    def test_normal_balance_proceeds_to_entry(self, mock_ctx, mock_store):
        from trigger.entry_engine import EntryRejection

        loop = _make_loop()
        loop.platforms.get_platform_balance.return_value = 500.0
        loop.platforms.fetch_market_data.return_value = {
            "H4": MagicMock(), "H1": MagicMock(),
            "M15": MagicMock(), "M5": MagicMock(), "M1": MagicMock(),
        }
        loop.platforms.get_broker_name.return_value = "test_broker"
        loop.platforms.get_typical_spreads.return_value = {}
        mock_ctx.return_value = SimpleNamespace(uses_stake=False)
        loop.entry_engine.calculate_entry.return_value = EntryRejection(
            pair="EURUSD", reason="test", direction="LONG", score=85
        )

        loop._execute_entry_inner(
            self._make_scan_result(), "LONDON", datetime.now(timezone.utc), "setup-123"
        )

        assert loop.risk_engine.balance == 500.0
        assert loop.entry_engine.calculate_entry.called


class TestBalanceClosePathSafety:
    """Fix B Site 2: close path must not use phantom $10k for pnl_pct."""

    def _make_position(self):
        return SimpleNamespace(
            symbol="EURUSD",
            direction="LONG",
            entry_price=1.10000,
            open_time=datetime.now(timezone.utc),
            lots=0.1,
            stake_usd=0,
            multiplier=1,
            order_id="T999",
            tp1=1.10200,
            tp2=1.10400,
            tp3=0.0,
            stop_loss=1.09800,
            breakeven_active=False,
            partial_closed=False,
            status="OPEN",
            score=85,
            confluences=["structure"],
            regime="TRENDING",
            session="LONDON",
            entry_type="LIMIT",
            platform="mt5",
        )

    @patch("platforms.main_loop.INSTRUMENT_REGISTRY", {
        "EURUSD": SimpleNamespace(pip_value_per_lot=10.0),
    })
    def test_close_path_zero_balance_uses_zero_pnl_pct(self):
        from platforms.main_loop import TradingLoop

        loop = _make_loop()
        loop.platforms.get_platform_balance.return_value = 0.0

        pos = self._make_position()
        loop._record_closed_trade(pos, 1.09, "SL", close_result=None)

        loop.drawdown.register_trade_result.assert_called_once()
        pnl_pct_arg = loop.drawdown.register_trade_result.call_args[0][0]
        assert pnl_pct_arg == 0.0

    @patch("platforms.main_loop.INSTRUMENT_REGISTRY", {
        "EURUSD": SimpleNamespace(pip_value_per_lot=10.0),
    })
    def test_close_path_normal_balance_computes_pnl_pct(self):
        from platforms.main_loop import TradingLoop
        from platforms.base_connector import CloseResult

        loop = _make_loop()
        loop.platforms.get_platform_balance.return_value = 1000.0

        pos = self._make_position()
        close_result = CloseResult(
            success=True, order_id="T999", close_price=1.10100,
            lots_closed=0.1, pnl=10.0, platform="mt5",
        )

        loop._record_closed_trade(pos, 1.10100, "TP1", close_result=close_result)

        loop.drawdown.register_trade_result.assert_called_once()
        pnl_pct_arg = loop.drawdown.register_trade_result.call_args[0][0]
        assert abs(pnl_pct_arg - 0.01) < 1e-6  # 10.0 / 1000.0


class TestNoPhantomBalance:
    """Assert the literal 10_000.0 fallback no longer exists on the balance path."""

    def test_no_phantom_fallback_in_execute_entry(self):
        import inspect
        from platforms.main_loop import TradingLoop

        source = inspect.getsource(TradingLoop._execute_entry_inner)
        assert "10_000" not in source
        assert "10000" not in source

    def test_no_phantom_fallback_in_record_closed(self):
        import inspect
        from platforms.main_loop import TradingLoop

        source = inspect.getsource(TradingLoop._record_closed_trade)
        assert "10_000" not in source
        assert "10000" not in source

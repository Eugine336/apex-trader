"""
Tests for PR-D: startup crash fix — broker-symbol resolution + fault isolation.

Covers:
  (a) resolve_to_internal: Deriv 1HZ##V family, identity for internal/forex, unknown passthrough.
  (b) Orphan with broker-native synthetic symbol adopted without raising.
  (c) Orphan whose symbol cannot be resolved and is not in registry → SKIPPED with
      [RECONCILE_SKIP], _reconcile_positions completes.
  (d) Zero-lot orphan is skipped.
"""

import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from brain.symbol_mapper import resolve_to_internal
from platforms.base_connector import OrderResult, PositionInfo


# ── (a) resolve_to_internal ─────────────────────────────────────────────


class TestResolveToInternal:

    def test_deriv_v50(self):
        assert resolve_to_internal("1HZ50V") == "V50_1S"

    def test_deriv_v10(self):
        assert resolve_to_internal("1HZ10V") == "V10_1S"

    def test_deriv_v25(self):
        assert resolve_to_internal("1HZ25V") == "V25_1S"

    def test_deriv_v75(self):
        assert resolve_to_internal("1HZ75V") == "V75_1S"

    def test_deriv_v100(self):
        assert resolve_to_internal("1HZ100V") == "V100_1S"

    def test_already_internal_key(self):
        assert resolve_to_internal("V50_1S") == "V50_1S"

    def test_forex_passthrough(self):
        assert resolve_to_internal("EURUSD") == "EURUSD"

    def test_unknown_symbol_passthrough(self):
        result = resolve_to_internal("TOTALLY_UNKNOWN_XYZ")
        assert result == "TOTALLY_UNKNOWN_XYZ"

    def test_deriv_stpidx(self):
        assert resolve_to_internal("stpRNG") == "STPIDX"

    def test_deriv_rdbull(self):
        assert resolve_to_internal("RDBULL") == "RNGBULL"

    def test_deriv_rdbear(self):
        assert resolve_to_internal("RDBEAR") == "RNGBEAR"

    def test_deriv_forex_frx(self):
        assert resolve_to_internal("frxEURUSD") == "EURUSD"

    def test_case_insensitive_internal(self):
        assert resolve_to_internal("eurusd") == "EURUSD"


# ── Helpers for reconciliation tests ────────────────────────────────────


def _make_position_info(
    oid="999",
    symbol="EURUSD",
    direction="BUY",
    lots=0.10,
    open_price=1.1000,
    sl=1.0980,
    tp=1.1020,
    platform="deriv",
):
    return PositionInfo(
        order_id=oid,
        symbol=symbol,
        direction=direction,
        lots=lots,
        open_price=open_price,
        current_price=open_price,
        sl=sl,
        tp=tp,
        pnl=0.0,
        swap=0.0,
        open_time=datetime.now(timezone.utc),
        platform=platform,
    )


def _build_minimal_loop():
    """Create a minimal TradingLoop with mocked deps, no real broker."""
    from platforms.main_loop import TradingLoop
    from management.trade_manager import TradeManager

    with patch.object(TradingLoop, "__init__", lambda self, **kw: None):
        loop = TradingLoop()

    loop.platforms = MagicMock()
    loop.trade_manager = TradeManager(
        partial_close_ratio=0.5, breakeven_buffer_pips=2.0
    )
    loop.position_store = MagicMock()
    loop.managed_positions = {}
    loop._position_scores = {}
    loop._recovery_completed = False
    loop._reconcile_interval_seconds = 30
    loop._last_reconcile_time = datetime.now(timezone.utc)
    loop.config = SimpleNamespace(risk=SimpleNamespace(
        reconcile_max_unconfirmed_cycles=20,
    ))
    return loop


def _mock_snapshot(positions, confirmed=None, failed=None):
    from platforms.platform_manager import BrokerPositionsSnapshot
    return BrokerPositionsSnapshot(
        positions=positions,
        confirmed_platforms=confirmed or {"mt5", "deriv"},
        failed_platforms=failed or set(),
    )


# ── (b) Broker-native synthetic adopted without raising ─────────────────


class TestOrphanBrokerNativeAdoption:

    def test_deriv_1hz50v_adopted_successfully(self):
        loop = _build_minimal_loop()
        orphan = _make_position_info(
            oid="deriv_123",
            symbol="1HZ50V",
            direction="SELL",
            lots=0.50,
            open_price=1234.56,
            sl=1240.0,
            tp=1220.0,
            platform="deriv",
        )
        loop.platforms.get_open_positions_snapshot.return_value = _mock_snapshot(
            [orphan]
        )

        loop._reconcile_positions()

        assert "deriv_123" in loop.managed_positions
        adopted = loop.managed_positions["deriv_123"]
        assert adopted.symbol == "V50_1S"
        assert adopted.direction == "SELL"
        assert adopted.entry_type == "ORPHAN_ADOPTED"
        loop.position_store.save_position.assert_called_once()

    def test_adopted_position_has_valid_pip_size(self):
        from config import get_pip_size

        loop = _build_minimal_loop()
        orphan = _make_position_info(
            oid="deriv_456",
            symbol="1HZ75V",
            direction="BUY",
            lots=1.0,
            open_price=5000.0,
            sl=4950.0,
            tp=5100.0,
            platform="deriv",
        )
        loop.platforms.get_open_positions_snapshot.return_value = _mock_snapshot(
            [orphan]
        )

        loop._reconcile_positions()

        adopted = loop.managed_positions["deriv_456"]
        pip = get_pip_size(adopted.symbol)
        assert pip > 0


# ── (c) Unresolvable symbol SKIPPED, reconciliation completes ───────────


class TestUnresolvableOrphanSkipped:

    def test_unknown_symbol_skipped_reconcile_completes(self):
        loop = _build_minimal_loop()
        orphan = _make_position_info(
            oid="orphan_bad",
            symbol="COMPLETELY_UNKNOWN_SYMBOL_XYZ",
            direction="BUY",
            lots=1.0,
            open_price=100.0,
            sl=95.0,
            tp=110.0,
            platform="deriv",
        )
        loop.platforms.get_open_positions_snapshot.return_value = _mock_snapshot(
            [orphan]
        )

        loop._reconcile_positions()

        assert "orphan_bad" not in loop.managed_positions
        loop.position_store.save_position.assert_not_called()

    def test_mixed_good_and_bad_orphans(self):
        loop = _build_minimal_loop()
        good = _make_position_info(
            oid="good_1",
            symbol="1HZ50V",
            direction="BUY",
            lots=0.50,
            open_price=1000.0,
            sl=990.0,
            tp=1020.0,
            platform="deriv",
        )
        bad = _make_position_info(
            oid="bad_1",
            symbol="NONEXISTENT_SYMBOL_ABC",
            direction="SELL",
            lots=0.10,
            open_price=500.0,
            sl=510.0,
            tp=480.0,
            platform="deriv",
        )
        loop.platforms.get_open_positions_snapshot.return_value = _mock_snapshot(
            [good, bad]
        )

        loop._reconcile_positions()

        assert "good_1" in loop.managed_positions
        assert "bad_1" not in loop.managed_positions


# ── (d) Zero-lot orphan skipped ─────────────────────────────────────────


class TestZeroLotOrphanSkipped:

    def test_zero_lot_orphan_skipped(self):
        loop = _build_minimal_loop()
        orphan = _make_position_info(
            oid="zero_lot_1",
            symbol="1HZ50V",
            direction="SELL",
            lots=0.00,
            open_price=1234.0,
            sl=1240.0,
            tp=1220.0,
            platform="deriv",
        )
        loop.platforms.get_open_positions_snapshot.return_value = _mock_snapshot(
            [orphan]
        )

        loop._reconcile_positions()

        assert "zero_lot_1" not in loop.managed_positions
        loop.position_store.save_position.assert_not_called()

    def test_negative_lot_orphan_skipped(self):
        loop = _build_minimal_loop()
        orphan = _make_position_info(
            oid="neg_lot",
            symbol="EURUSD",
            direction="BUY",
            lots=-0.01,
            open_price=1.1000,
            sl=1.0980,
            tp=1.1020,
            platform="mt5",
        )
        loop.platforms.get_open_positions_snapshot.return_value = _mock_snapshot(
            [orphan]
        )

        loop._reconcile_positions()

        assert "neg_lot" not in loop.managed_positions

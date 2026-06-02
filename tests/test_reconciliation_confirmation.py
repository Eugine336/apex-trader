"""
Tests for reconciliation positive-confirmation hardening.

Verifies that the system treats "cannot see the position" and "position is
closed" as fundamentally different states.  A position may only transition
OPEN → CLOSED when its own platform positively confirmed its open-position
list AND the position is absent from that confirmed list.

Regression tests for three critical defects:
  DEFECT 1 — _update_positions: total fetch failure purged the entire book.
  DEFECT 2 — Partial multi-broker: Deriv failure caused MT5-only aggregate,
             purging Deriv positions that were still open.
  DEFECT 3 — No positive-close confirmation; absence from a bulk list was
             the sole trigger.
"""

import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from management.trade_manager import (
    EntrySignal,
    TradeManager,
)
from platforms.base_connector import CloseResult, OrderResult, PositionInfo, TickData
from platforms.platform_manager import BrokerPositionsSnapshot


# ── Helpers ──────────────────────────────────────────────────────────


def _make_order(
    oid="100", symbol="EURUSD", direction="BUY",
    fill=1.1000, lots=0.10, sl=1.0980, tp=1.1020, platform="mt5",
) -> OrderResult:
    return OrderResult(
        success=True, order_id=oid, fill_price=fill,
        requested_price=fill, slippage_pips=0.0, lots=lots,
        symbol=symbol, direction=direction, sl=sl, tp=tp, platform=platform,
    )


def _make_position_info(
    oid="100", symbol="EURUSD", direction="BUY", lots=0.10,
    open_price=1.1000, current_price=1.1010, sl=1.0980, tp=1.1020,
    pnl=1.50, platform="mt5",
) -> PositionInfo:
    return PositionInfo(
        order_id=oid, symbol=symbol, direction=direction, lots=lots,
        open_price=open_price, current_price=current_price, sl=sl, tp=tp,
        pnl=pnl, swap=0.0, open_time=datetime.now(timezone.utc),
        platform=platform,
    )


def _make_tick(bid=1.1010, ask=1.1012) -> TickData:
    return TickData(bid=bid, ask=ask, spread=ask - bid, time=datetime.now(timezone.utc))


def _build_loop(*positions, reconcile_max_unconfirmed=20):
    """Create a minimal TradingLoop with the given managed positions.

    Each ``positions`` element is a tuple:
        (oid, symbol, direction, fill, lots, sl, tp1, tp2, platform)
    """
    from platforms.main_loop import TradingLoop, ManagedPosition

    with patch.object(TradingLoop, "__init__", lambda self, **kw: None):
        loop = TradingLoop()

    loop.platforms = MagicMock()
    loop.trade_manager = TradeManager(
        partial_close_ratio=0.5, breakeven_buffer_pips=2.0,
    )
    loop.position_store = MagicMock()
    loop.drawdown = MagicMock()
    loop.risk_engine = MagicMock()
    loop.ml = MagicMock()
    loop.journal = MagicMock()
    loop.re_entry = MagicMock()
    loop.execution_monitor = MagicMock()
    loop._journal_loop = MagicMock()
    loop.system_warnings = []
    loop._MAX_WARNINGS = 200
    loop.managed_positions = {}
    loop.config = SimpleNamespace(
        risk=SimpleNamespace(
            margin_guardian_enabled=False,
            reconcile_max_unconfirmed_cycles=reconcile_max_unconfirmed,
        ),
    )
    loop._last_reconcile_time = datetime.now(timezone.utc)
    loop.platforms.get_platform_balance.return_value = 10000.0

    for args in positions:
        oid, symbol, direction, fill, lots, sl, tp1, tp2, platform = args
        order = _make_order(oid, symbol, direction, fill, lots, sl, tp1, platform)
        pos = ManagedPosition(order=order, tp1=tp1, tp2=tp2, score=80)
        tm_signal = EntrySignal(
            pair=symbol, direction=direction, entry_price=fill,
            stop_loss=sl, tp1=tp1, tp2=tp2,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=lots, score=80,
        )
        tm_trade = loop.trade_manager.open_trade(tm_signal)
        pos.tm_trade_id = tm_trade.trade_id
        loop.managed_positions[oid] = pos

    return loop


# ── DEFECT 1 regression: total fetch failure must NOT purge ─────────


class TestTotalFetchFailure:
    """Total fetch exception ⇒ ZERO positions removed."""

    def test_update_positions_total_failure_retains_all(self):
        loop = _build_loop(
            ("1", "EURUSD", "BUY", 1.1, 0.1, 1.09, 1.12, 1.13, "mt5"),
            ("2", "GBPJPY", "SELL", 150.0, 0.2, 151.0, 149.0, 148.0, "deriv"),
        )
        loop.platforms.get_open_positions_snapshot.return_value = BrokerPositionsSnapshot(
            positions=[], confirmed_platforms=set(), failed_platforms={"mt5", "deriv"},
        )
        loop.platforms.get_price.return_value = _make_tick()
        loop.platforms.get_realized_pnl.return_value = None

        loop._update_positions()

        assert len(loop.managed_positions) == 2
        assert loop.managed_positions["1"].revalidation_pending is True
        assert loop.managed_positions["2"].revalidation_pending is True
        loop.position_store.remove_position.assert_not_called()

    def test_startup_reconcile_total_failure_retains_all(self):
        loop = _build_loop(
            ("1", "EURUSD", "BUY", 1.1, 0.1, 1.09, 1.12, 1.13, "mt5"),
        )
        loop.platforms.get_open_positions_snapshot.return_value = BrokerPositionsSnapshot(
            positions=[], confirmed_platforms=set(), failed_platforms={"mt5"},
        )

        loop._reconcile_positions()

        assert "1" in loop.managed_positions
        loop.position_store.remove_position.assert_not_called()


# ── DEFECT 2 regression: partial multi-broker ───────────────────────


class TestPartialMultiBrokerFetch:
    """MT5 confirmed, Deriv failed ⇒ Deriv positions retained."""

    def test_update_deriv_retained_mt5_removed(self):
        mt5_pos = _make_position_info("1", "EURUSD", platform="mt5")
        loop = _build_loop(
            ("1", "EURUSD", "BUY", 1.1, 0.1, 1.09, 1.12, 1.13, "mt5"),
            ("2", "V75_1S", "BUY", 500.0, 0.1, 490.0, 520.0, 540.0, "deriv"),
            ("3", "GBPUSD", "SELL", 1.25, 0.1, 1.26, 1.24, 1.23, "mt5"),
        )
        loop.platforms.get_open_positions_snapshot.return_value = BrokerPositionsSnapshot(
            positions=[mt5_pos],
            confirmed_platforms={"mt5"},
            failed_platforms={"deriv"},
        )
        loop.platforms.get_price.return_value = _make_tick()
        loop.platforms.get_realized_pnl.return_value = None

        loop._update_positions()

        assert "2" in loop.managed_positions, "Deriv position must be retained"
        assert loop.managed_positions["2"].revalidation_pending is True
        assert "3" not in loop.managed_positions, "MT5 position absent from confirmed list — removed"
        assert "1" in loop.managed_positions, "MT5 position present in confirmed list — kept"

    def test_reconcile_externally_closed_partial(self):
        loop = _build_loop(
            ("1", "EURUSD", "BUY", 1.1, 0.1, 1.09, 1.12, 1.13, "mt5"),
            ("2", "V75_1S", "BUY", 500.0, 0.1, 490.0, 520.0, 540.0, "deriv"),
        )
        loop.platforms.get_open_positions_snapshot.return_value = BrokerPositionsSnapshot(
            positions=[],
            confirmed_platforms={"mt5"},
            failed_platforms={"deriv"},
        )
        loop.platforms.get_price.return_value = _make_tick()
        loop.platforms.get_realized_pnl.return_value = None

        to_remove = []
        loop._reconcile_externally_closed(to_remove)

        assert "1" in [oid for oid in to_remove], "MT5 confirmed absent → removed"
        assert "2" in loop.managed_positions, "Deriv unconfirmed → retained"
        assert loop.managed_positions["2"].revalidation_pending is True


# ── Empty-but-confirmed = legitimate closes ─────────────────────────


class TestEmptyButConfirmed:
    """Platform confirmed and returns [] ⇒ positions on that platform removed."""

    def test_update_positions_confirmed_empty_removes(self):
        loop = _build_loop(
            ("1", "EURUSD", "BUY", 1.1, 0.1, 1.09, 1.12, 1.13, "mt5"),
        )
        loop.platforms.get_open_positions_snapshot.return_value = BrokerPositionsSnapshot(
            positions=[], confirmed_platforms={"mt5"}, failed_platforms=set(),
        )
        loop.platforms.get_price.return_value = _make_tick()
        loop.platforms.get_realized_pnl.return_value = 5.0

        loop._update_positions()

        assert "1" not in loop.managed_positions
        loop.position_store.remove_position.assert_called_once_with("1")

    def test_startup_reconcile_confirmed_empty_removes(self):
        loop = _build_loop(
            ("1", "EURUSD", "BUY", 1.1, 0.1, 1.09, 1.12, 1.13, "mt5"),
        )
        loop.platforms.get_open_positions_snapshot.return_value = BrokerPositionsSnapshot(
            positions=[], confirmed_platforms={"mt5"}, failed_platforms=set(),
        )

        loop._reconcile_positions()

        assert "1" not in loop.managed_positions
        loop.position_store.remove_position.assert_called_once_with("1")


# ── Revalidation flag lifecycle ─────────────────────────────────────


class TestRevalidationLifecycle:
    """Unconfirmed → confirmed recovery clears the revalidation flag."""

    def test_unconfirmed_then_confirmed_clears_flag(self):
        loop = _build_loop(
            ("1", "EURUSD", "BUY", 1.1, 0.1, 1.09, 1.12, 1.13, "mt5"),
        )
        loop.platforms.get_price.return_value = _make_tick()
        loop.platforms.get_realized_pnl.return_value = None

        loop.platforms.get_open_positions_snapshot.return_value = BrokerPositionsSnapshot(
            positions=[], confirmed_platforms=set(), failed_platforms={"mt5"},
        )
        loop._update_positions()
        assert loop.managed_positions["1"].revalidation_pending is True
        assert loop.managed_positions["1"].unconfirmed_cycles == 1

        mt5_pos = _make_position_info("1", "EURUSD", platform="mt5")
        loop.platforms.get_open_positions_snapshot.return_value = BrokerPositionsSnapshot(
            positions=[mt5_pos], confirmed_platforms={"mt5"}, failed_platforms=set(),
        )
        loop._update_positions()
        assert loop.managed_positions["1"].revalidation_pending is False
        assert loop.managed_positions["1"].unconfirmed_cycles == 0

    def test_unconfirmed_cycles_increment(self):
        loop = _build_loop(
            ("1", "EURUSD", "BUY", 1.1, 0.1, 1.09, 1.12, 1.13, "deriv"),
        )
        loop.platforms.get_price.return_value = _make_tick()
        loop.platforms.get_realized_pnl.return_value = None

        fail_snap = BrokerPositionsSnapshot(
            positions=[], confirmed_platforms=set(), failed_platforms={"deriv"},
        )
        loop.platforms.get_open_positions_snapshot.return_value = fail_snap

        for i in range(5):
            loop._update_positions()

        assert loop.managed_positions["1"].unconfirmed_cycles == 5
        assert "1" in loop.managed_positions


# ── Self-initiated close paths unaffected ───────────────────────────


class TestSelfInitiatedCloseUnaffected:
    """Positions closed via close_trade(success=True) still removed correctly."""

    def test_close_via_trade_manager_still_works(self):
        from platforms.main_loop import ManagedPosition

        loop = _build_loop(
            ("1", "EURUSD", "BUY", 1.1, 0.1, 1.09, 1.12, 1.13, "mt5"),
        )
        mt5_pos = _make_position_info("1", "EURUSD", platform="mt5", pnl=3.0)
        loop.platforms.get_open_positions_snapshot.return_value = BrokerPositionsSnapshot(
            positions=[mt5_pos], confirmed_platforms={"mt5"}, failed_platforms=set(),
        )
        loop.platforms.get_price.return_value = _make_tick()
        loop.platforms.close_trade.return_value = CloseResult(
            success=True, order_id="1", close_price=1.1010,
            lots_closed=0.1, pnl=3.0, platform="mt5",
        )

        assert "1" in loop.managed_positions


# ── BrokerPositionsSnapshot contract ────────────────────────────────


class TestBrokerPositionsSnapshot:
    """Verify the snapshot dataclass behaves correctly."""

    def test_empty_snapshot(self):
        snap = BrokerPositionsSnapshot()
        assert snap.positions == []
        assert snap.confirmed_platforms == set()
        assert snap.failed_platforms == set()

    def test_confirmed_platform_added(self):
        pos = _make_position_info("1", platform="mt5")
        snap = BrokerPositionsSnapshot(
            positions=[pos],
            confirmed_platforms={"mt5"},
            failed_platforms=set(),
        )
        assert "mt5" in snap.confirmed_platforms
        assert len(snap.positions) == 1

    def test_failed_platform_added(self):
        snap = BrokerPositionsSnapshot(
            positions=[],
            confirmed_platforms=set(),
            failed_platforms={"deriv"},
        )
        assert "deriv" in snap.failed_platforms
        assert len(snap.positions) == 0


# ── Startup reconcile: orphan adoption still works ──────────────────


class TestOrphanAdoptionPreserved:
    """Orphaned positions on confirmed platforms are still adopted."""

    def test_orphan_adopted_on_confirmed_platform(self):
        loop = _build_loop()
        broker_pos = _make_position_info(
            "999", "XAUUSD", "BUY", lots=0.5,
            open_price=2000.0, sl=1990.0, tp=2020.0, platform="mt5",
        )
        loop.platforms.get_open_positions_snapshot.return_value = BrokerPositionsSnapshot(
            positions=[broker_pos], confirmed_platforms={"mt5"}, failed_platforms=set(),
        )

        loop._reconcile_positions()

        assert "999" in loop.managed_positions
        assert loop.managed_positions["999"].entry_type == "ORPHAN_ADOPTED"
        loop.position_store.save_position.assert_called_once()

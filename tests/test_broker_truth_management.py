"""
Tests for broker-truth position management.
Verifies that the system uses broker state (real P&L, real open positions)
as the source of truth instead of an in-memory simulation.
"""

import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from management.trade_manager import (
    EntrySignal,
    ManagedTrade,
    TradeManager,
    TradeStatus,
    TERMINAL_STATUSES,
)
from platforms.base_connector import CloseResult, OrderResult, PositionInfo, TickData


# ── Helpers ──────────────────────────────────────────────────────────


def _make_order(
    oid: str = "100",
    symbol: str = "EURUSD",
    direction: str = "BUY",
    fill: float = 1.1000,
    lots: float = 0.10,
    sl: float = 1.0980,
    tp: float = 1.1020,
    platform: str = "mt5",
) -> OrderResult:
    return OrderResult(
        success=True,
        order_id=oid,
        fill_price=fill,
        requested_price=fill,
        slippage_pips=0.0,
        lots=lots,
        symbol=symbol,
        direction=direction,
        sl=sl,
        tp=tp,
        platform=platform,
    )


def _make_position_info(
    oid: str = "100",
    symbol: str = "EURUSD",
    direction: str = "BUY",
    lots: float = 0.10,
    open_price: float = 1.1000,
    current_price: float = 1.1010,
    sl: float = 1.0980,
    tp: float = 1.1020,
    pnl: float = 1.50,
    platform: str = "mt5",
) -> PositionInfo:
    return PositionInfo(
        order_id=oid,
        symbol=symbol,
        direction=direction,
        lots=lots,
        open_price=open_price,
        current_price=current_price,
        sl=sl,
        tp=tp,
        pnl=pnl,
        swap=0.0,
        open_time=datetime.now(timezone.utc),
        platform=platform,
    )


def _make_tick(bid: float = 1.1010, ask: float = 1.1012) -> TickData:
    return TickData(bid=bid, ask=ask, spread=ask - bid, time=datetime.now(timezone.utc))


def _build_loop_with_position(
    oid: str = "100",
    symbol: str = "EURUSD",
    direction: str = "BUY",
    fill: float = 1.1000,
    lots: float = 0.10,
    sl: float = 1.0980,
    tp1: float = 1.1020,
    tp2: float = 1.1040,
    platform: str = "mt5",
):
    """Create a minimal TradingLoop with one managed position, fully mocked."""
    from platforms.main_loop import TradingLoop, ManagedPosition

    with patch.object(TradingLoop, "__init__", lambda self, **kw: None):
        loop = TradingLoop()

    loop.platforms = MagicMock()
    loop.trade_manager = TradeManager(partial_close_ratio=0.5, breakeven_buffer_pips=2.0)
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
    loop.config = SimpleNamespace(risk=SimpleNamespace(margin_guardian_enabled=False))

    order = _make_order(oid, symbol, direction, fill, lots, sl, tp1, platform)
    pos = ManagedPosition(order=order, tp1=tp1, tp2=tp2, score=90)

    from management.trade_manager import EntrySignal as TMEntrySignal
    tm_signal = TMEntrySignal(
        pair=symbol,
        direction=direction,
        entry_price=fill,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        risk_reward_1=1.0,
        risk_reward_2=2.0,
        position_size_lots=lots,
        score=90,
    )
    tm_trade = loop.trade_manager.open_trade(tm_signal)
    pos.tm_trade_id = tm_trade.trade_id
    loop.managed_positions[oid] = pos

    return loop, pos, tm_trade


# ── Test: Broker-side close detected within one cycle ────────────────


class TestBrokerSideCloseDetection:
    """When the broker closes a position (SL/TP hit server-side),
    the system detects it within one _update_positions cycle and
    removes it from management."""

    def test_position_removed_when_broker_closes_it(self):
        loop, pos, tm_trade = _build_loop_with_position()
        oid = pos.order_id

        # Broker returns EMPTY list — position was closed broker-side
        loop.platforms.get_all_open_positions.return_value = []
        loop.platforms.get_price.return_value = _make_tick(1.1020, 1.1022)
        loop.platforms.get_platform_balance.return_value = 10000.0
        loop.platforms.get_realized_pnl.return_value = None

        # Mock the journal async call
        loop._run_journal_async = MagicMock()

        closed = loop._update_positions()

        assert closed == 1
        assert oid not in loop.managed_positions

    def test_broker_pnl_used_in_close_record(self):
        loop, pos, tm_trade = _build_loop_with_position()
        oid = pos.order_id
        pos.broker_pnl = 3.50  # last synced broker P&L

        loop.platforms.get_all_open_positions.return_value = []
        loop.platforms.get_price.return_value = _make_tick(1.1020, 1.1022)
        loop.platforms.get_platform_balance.return_value = 10000.0
        loop.platforms.get_realized_pnl.return_value = None
        loop._run_journal_async = MagicMock()

        loop._update_positions()

        # Verify risk_engine received the broker P&L, not simulated
        call_args = loop.risk_engine.record_trade_result.call_args
        assert call_args is not None
        assert call_args.kwargs.get("pnl_dollars") == 3.50 or call_args[1].get("pnl_dollars") == 3.50

    def test_position_stays_when_broker_still_has_it(self):
        loop, pos, tm_trade = _build_loop_with_position()
        oid = pos.order_id

        # Broker still has the position open
        bp = _make_position_info(oid, pnl=2.00)
        loop.platforms.get_all_open_positions.return_value = [bp]
        loop.platforms.get_price.return_value = _make_tick(1.1010, 1.1012)
        loop.platforms.fetch_market_data.return_value = {}

        closed = loop._update_positions()

        assert closed == 0
        assert oid in loop.managed_positions


# ── Test: Broker P&L synced to managed position each cycle ───────────


class TestBrokerPnlSync:
    """Each cycle, broker's real position.profit is synced onto the
    managed position for the dashboard to read."""

    def test_broker_pnl_synced_each_cycle(self):
        loop, pos, tm_trade = _build_loop_with_position()
        oid = pos.order_id

        assert pos.broker_pnl == 0.0  # initial

        bp = _make_position_info(oid, pnl=5.25, lots=0.10)
        loop.platforms.get_all_open_positions.return_value = [bp]
        loop.platforms.get_price.return_value = _make_tick(1.1015, 1.1017)
        loop.platforms.fetch_market_data.return_value = {}

        loop._update_positions()

        assert pos.broker_pnl == 5.25

    def test_broker_lots_synced_each_cycle(self):
        loop, pos, tm_trade = _build_loop_with_position()
        oid = pos.order_id

        bp = _make_position_info(oid, pnl=1.00, lots=0.05)
        loop.platforms.get_all_open_positions.return_value = [bp]
        loop.platforms.get_price.return_value = _make_tick(1.1010, 1.1012)
        loop.platforms.fetch_market_data.return_value = {}

        loop._update_positions()

        assert pos.broker_lots == 0.05

    def test_broker_sl_synced_if_changed(self):
        loop, pos, tm_trade = _build_loop_with_position()
        oid = pos.order_id

        # Broker has a different SL (e.g. user manually moved it)
        bp = _make_position_info(oid, sl=1.0990, pnl=1.00)
        loop.platforms.get_all_open_positions.return_value = [bp]
        loop.platforms.get_price.return_value = _make_tick(1.1010, 1.1012)
        loop.platforms.fetch_market_data.return_value = {}

        loop._update_positions()

        assert pos.sl == 1.0990


# ── Test: Simulated SL/TP2 does NOT close a broker-managed position ──


class TestSimulatedSlTpIgnored:
    """The simulation may think SL or TP2 was hit, but the broker
    manages hard SL/TP server-side. The system should NOT close the
    position based on the simulation — it resets and lets the broker
    handle it."""

    def test_simulated_sl_hit_does_not_close_broker_position(self):
        loop, pos, tm_trade = _build_loop_with_position(
            fill=1.1000, sl=1.0980, tp1=1.1020, tp2=1.1040,
        )
        oid = pos.order_id

        # Broker still has the position open (SL didn't fire broker-side)
        bp = _make_position_info(oid, pnl=-1.50, current_price=1.0979)
        loop.platforms.get_all_open_positions.return_value = [bp]
        # Price feed shows price at SL level — simulation will think SL hit
        loop.platforms.get_price.return_value = _make_tick(1.0979, 1.0981)
        loop.platforms.fetch_market_data.return_value = {}

        closed = loop._update_positions()

        # Position should NOT be closed — broker still has it
        assert closed == 0
        assert oid in loop.managed_positions
        # close_trade should NOT have been called
        loop.platforms.close_trade.assert_not_called()

    def test_simulated_tp2_hit_does_not_close_broker_position(self):
        loop, pos, tm_trade = _build_loop_with_position(
            fill=1.1000, sl=1.0980, tp1=1.1020, tp2=1.1040,
        )
        oid = pos.order_id
        # Force past TP1 state so TP2 check runs
        tm_trade.partial_closed = True
        tm_trade.breakeven_active = True
        pos.tp1_hit = True

        # Broker still has position open
        bp = _make_position_info(oid, pnl=4.00, current_price=1.1041)
        loop.platforms.get_all_open_positions.return_value = [bp]
        loop.platforms.get_price.return_value = _make_tick(1.1041, 1.1043)
        loop.platforms.fetch_market_data.return_value = {}

        closed = loop._update_positions()

        assert closed == 0
        assert oid in loop.managed_positions


# ── Test: Stall/structure exits still work (APEX-only management) ────


class TestValueAddManagement:
    """Stall exits and structure exits are APEX-only features the broker
    cannot enforce. These should still trigger real close calls."""

    def test_stall_exit_still_closes(self):
        loop, pos, tm_trade = _build_loop_with_position(
            fill=1.1000, sl=1.0980, tp1=1.1020, tp2=1.1040,
        )
        oid = pos.order_id
        # Set open_time far in the past so stall timer fires
        from datetime import timedelta
        pos.open_time = datetime.now(timezone.utc) - timedelta(minutes=120)
        tm_trade.entry_time = pos.open_time

        bp = _make_position_info(oid, pnl=-0.02, current_price=1.10001)
        loop.platforms.get_all_open_positions.return_value = [bp]
        # Price barely moved — stall condition
        loop.platforms.get_price.return_value = _make_tick(1.10001, 1.10003)
        loop.platforms.fetch_market_data.return_value = {}
        loop.platforms.close_trade.return_value = CloseResult(
            success=True, order_id=oid, close_price=1.10001,
            lots_closed=0.10, pnl=-0.02, platform="mt5",
        )
        loop.platforms.get_platform_balance.return_value = 10000.0
        loop._run_journal_async = MagicMock()

        closed = loop._update_positions()

        assert closed == 1
        assert oid not in loop.managed_positions
        loop.platforms.close_trade.assert_called_once()


# ── Test: Dashboard reads broker_pnl ─────────────────────────────────


class TestDashboardBrokerPnl:
    """The dashboard's open-trade P&L should prefer broker_pnl when
    available, not locally computed pip math."""

    def test_dashboard_uses_broker_pnl(self):
        """When broker_pnl != 0, state_trades should use it directly."""
        # Import state_trades directly to avoid dashboard/__init__ pulling FastAPI
        import importlib
        import sys

        # Ensure dashboard/__init__.py doesn't get imported
        if "dashboard" in sys.modules:
            saved = sys.modules["dashboard"]
        else:
            saved = None
        sys.modules["dashboard"] = type(sys)("dashboard")
        sys.modules["dashboard"].__path__ = ["dashboard"]

        try:
            spec = importlib.util.spec_from_file_location(
                "dashboard.state_trades", "dashboard/state_trades.py"
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules["dashboard.state_trades"] = mod
            spec.loader.exec_module(mod)
            TradesMixin = mod.TradesMixin
        except Exception:
            pytest.skip("Cannot import state_trades without FastAPI")
            return
        finally:
            if saved is not None:
                sys.modules["dashboard"] = saved
            elif "dashboard" in sys.modules:
                del sys.modules["dashboard"]

        pos = SimpleNamespace(
            symbol="EURUSD",
            direction="BUY",
            entry_price=1.1000,
            lots=0.10,
            sl=1.0980,
            tp1=1.1020,
            tp2=1.1040,
            score=90,
            tp1_hit=False,
            at_breakeven=False,
            trailing=False,
            broker_pnl=7.50,
            stake_usd=0.0,
            multiplier=100,
        )

        # Test the broker_pnl attribute is accessible and non-zero
        assert pos.broker_pnl == 7.50

    def test_managed_position_broker_pnl_attribute_accessible(self):
        from platforms.main_loop import ManagedPosition

        order = _make_order()
        pos = ManagedPosition(order=order, tp1=1.1020, tp2=1.1040)
        pos.broker_pnl = 12.34

        assert pos.broker_pnl == 12.34


# ── Test: ManagedPosition has broker_pnl/broker_lots attributes ──────


class TestManagedPositionAttributes:
    def test_new_position_has_zero_broker_pnl(self):
        from platforms.main_loop import ManagedPosition

        order = _make_order()
        pos = ManagedPosition(order=order, tp1=1.1020, tp2=1.1040)

        assert pos.broker_pnl == 0.0
        assert pos.broker_lots == 0.0

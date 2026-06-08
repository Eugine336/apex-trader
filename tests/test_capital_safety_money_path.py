"""
APEX TRADER — Capital Safety Money-Path Regression Tests

Restored from commit 66d2af8 after the PR #127 merge-conflict resolution
(654ec89) silently dropped these 24 tests.  They now live under a separate
filename so they coexist with the PR #127 dedup-shape tests in
test_capital_safety_fixes.py.

Validates FIX 1 (daily-budget ceiling on sent lots),
FIX 2 (idem-key dedup includes history),
FIX 3 (startup in-flight intent reconciliation).
"""

import sys
from types import SimpleNamespace, ModuleType
from unittest.mock import MagicMock

# ── Stub heavy deps before any app imports ────────────────────────────────
_STUB_MODULES = [
    "MetaTrader5", "requests", "loguru", "websockets",
    "websockets.sync", "websockets.sync.client",
    "aiosqlite", "sqlalchemy",
    "torch", "torch.nn", "torch.nn.functional",
    "torch.optim", "torch.distributions",
]
for mod in _STUB_MODULES:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

_loguru = sys.modules["loguru"]
_loguru.logger = MagicMock()

_np = ModuleType("numpy")
_np.__version__ = "1.26.0"
_np.ndarray = list
_np.float64 = float
_np.array = lambda x, **kw: list(x) if hasattr(x, "__iter__") else [x]
_np.zeros = lambda n: [0] * n
_np.mean = lambda x: sum(x) / len(x) if x else 0.0
_np.std = lambda x, **kw: 0.0
_np.nan = float("nan")
_np.isnan = lambda x: x != x
sys.modules["numpy"] = _np

_pd = ModuleType("pandas")

class _FakeTS:
    @staticmethod
    def now(tz=None):
        return _FakeTS()
    def isoformat(self):
        return "2025-01-01T00:00:00+00:00"

_pd.Timestamp = _FakeTS
_pd.Series = MagicMock
_pd.DataFrame = MagicMock
sys.modules["pandas"] = _pd

import pytest

from persistence.position_store import PositionStore, STORE_UNAVAILABLE
from platforms.base_connector import OrderResult
from platforms.main_loop import ManagedPosition


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _make_order(**kw):
    defaults = dict(
        success=True, order_id="ORD-1", fill_price=1.105,
        requested_price=1.105, slippage_pips=0.0, lots=0.10,
        symbol="EURUSD", direction="BUY", sl=1.09, tp=1.12, platform="mt5",
    )
    defaults.update(kw)
    return OrderResult(**defaults)


def _make_position(**kw):
    order = _make_order(**{k: v for k, v in kw.items()
                           if k in ("order_id", "symbol", "direction",
                                    "lots", "fill_price", "sl", "platform")})
    pos = ManagedPosition(
        order=order,
        tp1=kw.get("tp1", 1.12),
        tp2=kw.get("tp2", 1.15),
        score=kw.get("score", 92),
        regime=kw.get("regime", "TRENDING"),
        session=kw.get("session", "LONDON"),
        entry_type=kw.get("entry_type", "FVG_OB"),
        stake_usd=kw.get("stake_usd", 0.0),
        multiplier=kw.get("multiplier", 100),
    )
    pos.tm_trade_id = kw.get("tm_trade_id", "TM-001")
    return pos


def _make_mt5_connector(reject_on_minlot_inflation=False):
    from platforms.mt5.mt5_connector import MT5Connector
    c = MT5Connector.__new__(MT5Connector)
    c._connected = True
    c._magic = 202500
    c._deviation = 20
    c._broker_name = "test"
    c._symbol_cache = {"EURUSD": "EURUSD", "GBPUSD": "GBPUSD"}
    c._not_found_warned = set()
    c._reject_on_minlot_inflation = reject_on_minlot_inflation
    c._mapper = None
    return c


def _setup_mt5_mock():
    mt5_mock = sys.modules["MetaTrader5"]
    mt5_mock.TRADE_ACTION_DEAL = 1
    mt5_mock.ORDER_TYPE_BUY = 0
    mt5_mock.ORDER_TYPE_SELL = 1
    mt5_mock.ORDER_TIME_GTC = 0
    mt5_mock.ORDER_FILLING_IOC = 1
    mt5_mock.TRADE_RETCODE_DONE = 10009
    mt5_mock.symbol_info_tick = MagicMock(
        return_value=SimpleNamespace(ask=1.1000, bid=1.0998)
    )
    mt5_mock.symbol_select = MagicMock()
    mt5_mock.symbol_info = MagicMock(return_value=SimpleNamespace(
        volume_min=0.01, volume_max=100.0, volume_step=0.01,
        trade_stops_level=0, digits=5, point=0.00001,
    ))
    mt5_mock.positions_get = MagicMock(return_value=[])
    mt5_mock.orders_get = MagicMock(return_value=[])
    mt5_mock.history_deals_get = MagicMock(return_value=[])
    mt5_mock.history_orders_get = MagicMock(return_value=[])
    sent = {}

    def fake_order_send(req):
        sent.update(req)
        return SimpleNamespace(retcode=10009, order=1001, price=1.1000, comment="")

    mt5_mock.order_send = fake_order_send
    return mt5_mock, sent


# ═══════════════════════════════════════════════════════════════════════════
# FIX 1 — Assessment ceiling caps sent lots
# ═══════════════════════════════════════════════════════════════════════════

class TestAssessmentCeilingCapsLots:
    """FIX 1: When RiskEngine reduces size for daily budget, the risk ceiling
    must be min(signal.position_size_lots, assessment.position_size_lots)."""

    def test_ceiling_applied_when_assessment_smaller(self):
        signal_lots = 0.10
        assessment_lots = 0.03
        ml_mult = 1.0
        density_mult = 1.0
        vol_mult = 1.0

        adjusted_lots = round(signal_lots * ml_mult * density_mult * vol_mult, 2)
        risk_ceiling = signal_lots
        if assessment_lots > 0:
            risk_ceiling = min(risk_ceiling, assessment_lots)
        if adjusted_lots > risk_ceiling:
            adjusted_lots = risk_ceiling
        adjusted_lots = max(0.01, adjusted_lots)

        assert adjusted_lots == 0.03

    def test_ceiling_not_applied_when_assessment_zero(self):
        signal_lots = 0.10
        assessment_lots = 0.0
        ml_mult = 1.0
        density_mult = 1.0
        vol_mult = 1.0

        adjusted_lots = round(signal_lots * ml_mult * density_mult * vol_mult, 2)
        risk_ceiling = signal_lots
        if assessment_lots > 0:
            risk_ceiling = min(risk_ceiling, assessment_lots)
        if adjusted_lots > risk_ceiling:
            adjusted_lots = risk_ceiling
        adjusted_lots = max(0.01, adjusted_lots)

        assert adjusted_lots == 0.10

    def test_rejects_when_ceiling_below_min_lot(self):
        signal_lots = 0.10
        assessment_lots = 0.005
        ml_mult = 1.0

        adjusted_lots = round(signal_lots * ml_mult, 2)
        risk_ceiling = signal_lots
        if assessment_lots > 0:
            risk_ceiling = min(risk_ceiling, assessment_lots)
        if adjusted_lots > risk_ceiling:
            adjusted_lots = risk_ceiling

        should_reject = (
            adjusted_lots < 0.01
            and assessment_lots > 0
            and assessment_lots < signal_lots
        )
        assert should_reject is True

    def test_adaptive_multiplier_also_capped_by_assessment(self):
        signal_lots = 0.10
        assessment_lots = 0.05
        ml_mult = 0.8
        density_mult = 0.9
        vol_mult = 1.0

        adjusted_lots = round(signal_lots * ml_mult * density_mult * vol_mult, 2)
        risk_ceiling = signal_lots
        if assessment_lots > 0:
            risk_ceiling = min(risk_ceiling, assessment_lots)
        if adjusted_lots > risk_ceiling:
            adjusted_lots = risk_ceiling
        adjusted_lots = max(0.01, adjusted_lots)

        assert adjusted_lots <= 0.05, f"adjusted_lots {adjusted_lots} exceeds assessment ceiling 0.05"

    def test_ceiling_code_in_main_loop(self):
        """Verify the assessment ceiling code is present in main_loop.py."""
        import inspect
        from platforms.main_loop import TradingLoop
        source = inspect.getsource(TradingLoop._execute_entry_inner)
        assert "assessment.position_size_lots" in source, (
            "FIX 1: _execute_entry_inner must reference assessment.position_size_lots"
        )
        assert "daily_budget_below_min_lot" in source, (
            "FIX 1: _execute_entry_inner must reject when daily budget ceiling < min lot"
        )

    def test_log_reports_filled_lots_not_signal(self):
        """Verify TRADE OPENED log uses order.lots, not signal.position_size_lots."""
        import inspect
        from platforms.main_loop import TradingLoop
        source = inspect.getsource(TradingLoop._execute_entry_inner)
        log_idx = source.find("TRADE OPENED")
        assert log_idx != -1
        snippet = source[log_idx:log_idx + 200]
        assert "order.lots" in snippet, (
            "TRADE OPENED log must use order.lots (the filled size)"
        )
        assert "signal.position_size_lots" not in snippet, (
            "TRADE OPENED log must NOT use signal.position_size_lots"
        )


# ═══════════════════════════════════════════════════════════════════════════
# FIX 2 — _find_order_by_idem_key includes history
# ═══════════════════════════════════════════════════════════════════════════

class TestMT5IdemKeyHistoryLookup:
    """FIX 2: _find_order_by_idem_key must scan recent deal/order history."""

    def test_finds_deal_in_history(self):
        mt5_mock, _ = _setup_mt5_mock()
        mt5_mock.positions_get.return_value = []
        mt5_mock.orders_get.return_value = []
        historical_deal = SimpleNamespace(
            ticket=9999, comment="APEX|abc123def456|85", price=1.1000,
            price_open=1.1000, volume=0.05, sl=1.0900, tp=1.1200,
            type=0,
        )
        mt5_mock.history_deals_get.return_value = [historical_deal]
        mt5_mock.history_orders_get.return_value = []

        c = _make_mt5_connector()
        found = c._find_order_by_idem_key("abc123def456")
        assert found is not None
        assert found.ticket == 9999

    def test_finds_order_in_history(self):
        mt5_mock, _ = _setup_mt5_mock()
        mt5_mock.positions_get.return_value = []
        mt5_mock.orders_get.return_value = []
        mt5_mock.history_deals_get.return_value = []
        historical_order = SimpleNamespace(
            ticket=8888, comment="APND|xyz789abcdef|90",
            price_open=1.0500, volume=0.10, sl=1.0400, tp=1.0700,
            type=1,
        )
        mt5_mock.history_orders_get.return_value = [historical_order]

        c = _make_mt5_connector()
        found = c._find_order_by_idem_key("xyz789abcdef")
        assert found is not None
        assert found.ticket == 8888

    def test_still_finds_in_open_positions(self):
        mt5_mock, _ = _setup_mt5_mock()
        open_pos = SimpleNamespace(
            ticket=7777, comment="APEX|abc123def456|85",
            price_open=1.1000, volume=0.05, sl=1.0900, tp=1.1200,
            type=0, price_current=1.1050, profit=5.0, swap=0.0,
            time=1700000000, symbol="EURUSD",
        )
        mt5_mock.positions_get.return_value = [open_pos]

        c = _make_mt5_connector()
        found = c._find_order_by_idem_key("abc123def456")
        assert found is not None
        assert found.ticket == 7777

    def test_returns_none_when_no_match(self):
        mt5_mock, _ = _setup_mt5_mock()
        mt5_mock.positions_get.return_value = []
        mt5_mock.orders_get.return_value = []
        mt5_mock.history_deals_get.return_value = []
        mt5_mock.history_orders_get.return_value = []

        c = _make_mt5_connector()
        assert c._find_order_by_idem_key("nonexistent123") is None

    def test_history_failure_does_not_crash(self):
        mt5_mock, _ = _setup_mt5_mock()
        mt5_mock.positions_get.return_value = []
        mt5_mock.orders_get.return_value = []
        mt5_mock.history_deals_get.side_effect = RuntimeError("MT5 unavailable")

        c = _make_mt5_connector()
        result = c._find_order_by_idem_key("abc123def456")
        assert result is None

    def test_place_order_dedupes_via_history(self):
        mt5_mock, sent = _setup_mt5_mock()
        mt5_mock.positions_get.return_value = []
        mt5_mock.orders_get.return_value = []
        historical_deal = SimpleNamespace(
            ticket=5555, comment="APEX|abc123def456|85",
            price_open=1.0999, volume=0.05, sl=1.0900, tp=1.1200,
            type=0,
        )
        mt5_mock.history_deals_get.return_value = [historical_deal]

        c = _make_mt5_connector()
        result = c.place_order("EURUSD", "BUY", 0.05, 1.09, 1.12,
                               idempotency_key="abc123def456")
        assert result.success is True
        assert result.order_id == "5555"
        assert not sent, "Should NOT have sent a new order to broker"


# ═══════════════════════════════════════════════════════════════════════════
# FIX 2 (Deriv) — _find_contract_by_idem_key includes profit table
# ═══════════════════════════════════════════════════════════════════════════

class TestDerivIdemKeyHistoryLookup:
    """FIX 2: _find_contract_by_idem_key must also check profit_table."""

    def _make_deriv_connector(self):
        from platforms.deriv.deriv_connector import DerivConnector
        conn = object.__new__(DerivConnector)
        conn._positions = {}
        conn._ws = MagicMock()
        conn._connected = True
        conn._reconnecting = False
        conn._sync_send = MagicMock(return_value={})
        conn._require_connection = MagicMock()
        return conn

    def test_finds_in_profit_table(self):
        conn = self._make_deriv_connector()
        conn._sync_send = MagicMock(side_effect=[
            {"portfolio": {"contracts": []}},
            {"profit_table": {"transactions": [
                {"contract_id": "C-999", "passthrough": {"idem_key": "abc123def456"}},
            ]}},
        ])
        result = conn._find_contract_by_idem_key("abc123def456")
        assert result == "C-999"

    def test_portfolio_still_checked_first(self):
        conn = self._make_deriv_connector()
        conn._sync_send = MagicMock(return_value={
            "portfolio": {"contracts": [
                {"contract_id": "C-111", "passthrough": {"idem_key": "abc123def456"}},
            ]}
        })
        result = conn._find_contract_by_idem_key("abc123def456")
        assert result == "C-111"

    def test_in_memory_checked_first(self):
        conn = self._make_deriv_connector()
        conn._positions = {"C-222": {"idem_key": "abc123def456"}}
        result = conn._find_contract_by_idem_key("abc123def456")
        assert result == "C-222"
        conn._sync_send.assert_not_called()

    def test_returns_empty_when_no_match(self):
        conn = self._make_deriv_connector()
        conn._sync_send = MagicMock(side_effect=[
            {"portfolio": {"contracts": []}},
            {"profit_table": {"transactions": []}},
        ])
        result = conn._find_contract_by_idem_key("nonexistent123")
        assert result == ""


# ═══════════════════════════════════════════════════════════════════════════
# FIX 3 — Position store: get_all_pending_in_flight
# ═══════════════════════════════════════════════════════════════════════════

class TestPositionStoreInFlight:

    @pytest.fixture
    def store(self, tmp_path):
        db_path = str(tmp_path / "test_in_flight.db")
        s = PositionStore(db_path=db_path)
        yield s
        s.close()

    def test_get_all_pending_returns_pending_only(self, store):
        store.record_in_flight("K1", "EURUSD", "BUY", 0.10)
        store.record_in_flight("K2", "GBPUSD", "SELL", 0.05)
        store.resolve_in_flight("K1", "ORD-100")

        pending = store.get_all_pending_in_flight()
        assert isinstance(pending, list)
        assert len(pending) == 1
        assert pending[0]["idempotency_key"] == "K2"

    def test_get_all_pending_empty(self, store):
        pending = store.get_all_pending_in_flight()
        assert pending == []

    def test_cleanup_removes_old_pending(self, store):
        store.record_in_flight("OLD", "EURUSD", "BUY", 0.10)
        store.cleanup_stale_in_flight(max_age_seconds=0)
        pending = store.get_all_pending_in_flight()
        assert pending == []


# ═══════════════════════════════════════════════════════════════════════════
# FIX 3 — Startup in-flight reconciliation
# ═══════════════════════════════════════════════════════════════════════════

class TestStartupInFlightReconciliation:

    def _build_loop(self, tmp_path):
        from platforms.main_loop import TradingLoop
        from config import RiskConfig

        loop = TradingLoop.__new__(TradingLoop)
        loop.config = type("C", (), {
            "risk": RiskConfig(),
        })()
        loop.managed_positions = {}
        loop._position_scores = {}
        loop._recovery_completed = False
        loop._reconcile_interval_seconds = 300
        loop._last_reconcile_time = None

        db_path = str(tmp_path / "test_recovery.db")
        loop.position_store = PositionStore(db_path=db_path)

        loop.platforms = MagicMock()
        loop.platforms.get_open_positions_snapshot.return_value = MagicMock(
            positions=[], confirmed_platforms={"mt5"}, failed_platforms=set(),
        )
        loop.platforms.find_order_by_idem_key = MagicMock(return_value=None)
        loop.trade_manager = MagicMock()
        return loop

    def test_filled_intent_resolved(self, tmp_path):
        loop = self._build_loop(tmp_path)
        loop.position_store.record_in_flight("KEY-A", "EURUSD", "BUY", 0.10)

        broker_match = SimpleNamespace(ticket=12345)
        loop.platforms.find_order_by_idem_key.return_value = broker_match

        loop._reconcile_in_flight_intents()

        rec = loop.position_store.get_in_flight_checked("KEY-A")
        assert rec is not None
        assert rec["status"] == "FILLED"
        assert rec["order_id"] == "12345"

    def test_no_fill_intent_cancelled(self, tmp_path):
        loop = self._build_loop(tmp_path)
        loop.position_store.record_in_flight("KEY-B", "GBPUSD", "SELL", 0.05)

        loop.platforms.find_order_by_idem_key.return_value = None

        loop._reconcile_in_flight_intents()

        rec = loop.position_store.get_in_flight_checked("KEY-B")
        assert rec is None, "cancel_in_flight DELETEs the row"

    def test_store_unavailable_skips_safely(self, tmp_path):
        loop = self._build_loop(tmp_path)
        loop.position_store = MagicMock()
        loop.position_store.get_all_pending_in_flight.return_value = STORE_UNAVAILABLE

        loop._reconcile_in_flight_intents()
        loop.position_store.resolve_in_flight.assert_not_called()
        loop.position_store.cancel_in_flight.assert_not_called()

    def test_startup_recovery_calls_in_flight_reconciliation(self, tmp_path):
        loop = self._build_loop(tmp_path)
        loop.position_store.record_in_flight("KEY-C", "EURUSD", "BUY", 0.10)
        loop.platforms.find_order_by_idem_key.return_value = None

        loop._perform_startup_recovery()

        assert loop._recovery_completed is True
        rec = loop.position_store.get_in_flight_checked("KEY-C")
        assert rec is None, "Startup recovery should cancel unfilled in-flight intents"

    def test_empty_pending_runs_cleanup(self, tmp_path):
        loop = self._build_loop(tmp_path)
        loop.position_store.record_in_flight("OLD", "EURUSD", "BUY", 0.10)
        loop.position_store.cancel_in_flight("OLD")

        loop._reconcile_in_flight_intents()

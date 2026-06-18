"""
Tests that all monetary constants are config-sourced, never hardcoded:
  A. PositionSizer honours injected micro_account_threshold_usd / deriv_min_stake_usd
  B. RiskEngine constructs PositionSizer with the config thresholds
  C. calculate_entry requires account_balance (no default)
  D. RiskEngine seed comes from cfg.risk.backtest_starting_balance_usd
"""

import sys
import inspect
from types import ModuleType
from unittest.mock import MagicMock

# ── stub heavy deps before any project imports ──────────────────────────
_STUB_MODULES = [
    "MetaTrader5", "requests", "websockets",
    "websockets.sync", "websockets.sync.client",
    "aiosqlite", "sqlalchemy",
    "torch", "torch.nn", "torch.nn.functional",
    "torch.optim", "torch.distributions",
]
for mod in _STUB_MODULES:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

_loguru = sys.modules.setdefault("loguru", MagicMock())
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
if "numpy" not in sys.modules:
    sys.modules["numpy"] = _np

_pd = MagicMock()
_pd.DataFrame = MagicMock
if "pandas" not in sys.modules:
    sys.modules["pandas"] = _pd

import pytest
from config import AppConfig, RiskConfig
from risk.position_sizer import PositionSizer
from risk.risk_engine import RiskEngine


# ── A. PositionSizer micro/deriv thresholds are instance-configurable ───


class TestPositionSizerThresholds:
    def test_default_thresholds(self):
        sizer = PositionSizer()
        assert sizer.micro_account_threshold_usd == 100.0
        assert sizer.deriv_min_stake_usd == 0.35

    def test_custom_thresholds(self):
        sizer = PositionSizer(
            micro_account_threshold_usd=250.0,
            deriv_min_stake_usd=1.00,
        )
        assert sizer.micro_account_threshold_usd == 250.0
        assert sizer.deriv_min_stake_usd == 1.00

    def test_micro_skip_boundary_shifts_with_config(self):
        sizer = PositionSizer(micro_account_threshold_usd=500.0)
        # max_loss 40 on a 400 account = 10% risk, above the 5% per-trade cap
        # → rejected. Account is below the micro threshold → micro label.
        result = sizer._adjust_for_account_size(
            lots=0.01,
            account_balance=400.0,
            risk_amount=2.0,
            max_loss=40.0,
        )
        assert result[0] == 0.0
        assert "micro" in result[1]

    def test_micro_skip_not_triggered_above_threshold(self):
        sizer = PositionSizer(micro_account_threshold_usd=200.0)
        # max_loss 40 on a 300 account = 13.3% risk, above the 5% cap →
        # rejected. Account is above the micro threshold → non-micro label.
        result = sizer._adjust_for_account_size(
            lots=0.01,
            account_balance=300.0,
            risk_amount=2.0,
            max_loss=40.0,
        )
        assert result[0] == 0.0
        assert "skip_min_lot_over_risk" in result[1]

    def test_micro_account_trades_when_min_lot_risk_within_cap(self):
        # max_loss 5 on a 400 account = 1.25% risk, within the 5% cap →
        # the trade is allowed at min lot even though it exceeds the soft
        # 1.5× risk_amount tolerance.
        sizer = PositionSizer(micro_account_threshold_usd=500.0)
        result = sizer._adjust_for_account_size(
            lots=0.01,
            account_balance=400.0,
            risk_amount=2.0,
            max_loss=5.0,
        )
        assert result[0] == 0.01
        assert result[1] == "lots"

    def test_deriv_min_stake_boundary_shifts_with_config(self):
        sizer = PositionSizer(
            micro_account_threshold_usd=100.0,
            deriv_min_stake_usd=2.00,
        )
        result = sizer.calculate_stake(
            account_balance=50.0,
            risk_pct=0.01,
            entry_price=1.0,
            stop_loss=0.99,
        )
        assert result.stake_usd == 0.0
        assert "skip_micro" in result.sizing_mode


# ── B. RiskEngine passes config thresholds to PositionSizer ─────────────


class TestRiskEngineConfigFlow:
    def test_sizer_receives_config_thresholds(self):
        cfg = AppConfig()
        cfg.risk.micro_account_threshold_usd = 300.0
        cfg.risk.deriv_min_stake_usd = 0.75
        engine = RiskEngine(config=cfg, starting_balance=10_000.0)
        assert engine.position_sizer.micro_account_threshold_usd == 300.0
        assert engine.position_sizer.deriv_min_stake_usd == 0.75

    def test_default_config_flows_defaults(self):
        engine = RiskEngine(starting_balance=10_000.0)
        assert engine.position_sizer.micro_account_threshold_usd == 100.0
        assert engine.position_sizer.deriv_min_stake_usd == 0.35


# ── C. calculate_entry requires account_balance (no default) ────────────


class TestEntryEngineBalanceRequired:
    def test_account_balance_is_required_kwarg(self):
        from trigger.entry_engine import EntryEngine
        sig = inspect.signature(EntryEngine.calculate_entry)
        param = sig.parameters["account_balance"]
        assert param.default is inspect.Parameter.empty, (
            "account_balance must be a required parameter, not defaulted"
        )


# ── D. RiskEngine seed from config ──────────────────────────────────────


class TestRiskEngineSeedFromConfig:
    def test_seed_from_config_when_no_explicit_balance(self):
        cfg = AppConfig()
        cfg.risk.backtest_starting_balance_usd = 25_000.0
        engine = RiskEngine(config=cfg)
        assert engine.balance == 25_000.0

    def test_explicit_balance_overrides_config(self):
        cfg = AppConfig()
        cfg.risk.backtest_starting_balance_usd = 25_000.0
        engine = RiskEngine(config=cfg, starting_balance=5_000.0)
        assert engine.balance == 5_000.0

    def test_default_config_seed_is_10k(self):
        engine = RiskEngine()
        assert engine.balance == 10_000.0


# ── E. RiskConfig fields exist with correct defaults ────────────────────


class TestRiskConfigMoneyFields:
    def test_micro_account_threshold_field(self):
        cfg = RiskConfig()
        assert cfg.micro_account_threshold_usd == 100.0

    def test_deriv_min_stake_field(self):
        cfg = RiskConfig()
        assert cfg.deriv_min_stake_usd == 0.35

    def test_backtest_starting_balance_field(self):
        cfg = RiskConfig()
        assert cfg.backtest_starting_balance_usd == 10_000.0

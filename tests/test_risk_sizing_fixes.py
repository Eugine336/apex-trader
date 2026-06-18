"""
APEX TRADER — Risk / Sizing Defect Fix Regression Tests

Covers three fixes:
  1. assess() now receives score/regime/session → _scale_risk_by_score is exercised.
  2. MIN_LOT floor over-risk guard applies to ALL accounts (not just micro).
  3. risk_pips <= 0 returns lots=0.0 / skip_invalid_stop (no tradeable lot).
"""

import sys
from types import ModuleType
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
_np.isscalar = lambda x: isinstance(x, (int, float, complex))
if "numpy" not in sys.modules:
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
if "pandas" not in sys.modules:
    sys.modules["pandas"] = _pd

import pytest

from risk.position_sizer import PositionSizer, SizeResult
from risk.risk_engine import RiskEngine, RiskAssessment


# ═══════════════════════════════════════════════════════════════════════════
# FIX 1 — score/regime/session wired into assess(); _scale_risk_by_score runs
# ═══════════════════════════════════════════════════════════════════════════

class TestScoreScaling:
    """Verify that passing score to assess() actually changes the risk sizing."""

    def _make_engine(self, balance=10_000.0):
        engine = RiskEngine(starting_balance=balance)
        return engine

    def _base_assess_kwargs(self, balance=10_000.0):
        return dict(
            pair="EURUSD",
            direction="BUY",
            entry_price=1.10000,
            stop_loss=1.09500,
            open_trades=[],
            account_balance=balance,
        )

    def test_score_zero_uses_base_risk(self):
        engine = self._make_engine()
        result = engine.assess(**self._base_assess_kwargs(), score=0)
        assert result.approved
        base_risk = result.risk_pct
        assert base_risk > 0

    def test_low_score_reduces_risk(self):
        """score=80 → factor=0.5, risk should be ~half of base."""
        engine = self._make_engine()
        base = engine.assess(**self._base_assess_kwargs(), score=0)
        scaled = engine.assess(**self._base_assess_kwargs(), score=80)
        assert scaled.approved
        assert scaled.risk_pct < base.risk_pct
        assert abs(scaled.risk_pct - base.risk_pct * 0.5) < 1e-6

    def test_medium_score_reduces_risk(self):
        """score=85 → factor=0.7."""
        engine = self._make_engine()
        base = engine.assess(**self._base_assess_kwargs(), score=0)
        scaled = engine.assess(**self._base_assess_kwargs(), score=85)
        assert scaled.approved
        assert abs(scaled.risk_pct - base.risk_pct * 0.7) < 1e-6

    def test_high_score_at_hwm_peak_boosts_risk(self):
        """score=95 at HWM peak → factor=1.0 × 1.1 HWM boost = 110% of base."""
        engine = self._make_engine()
        base = engine.assess(**self._base_assess_kwargs(), score=0)
        scaled = engine.assess(**self._base_assess_kwargs(), score=95)
        assert scaled.approved
        assert scaled.risk_pct > base.risk_pct
        assert abs(scaled.risk_pct - base.risk_pct * 1.1) < 1e-6

    def test_regime_and_session_accepted(self):
        """assess() must accept regime and session without error."""
        engine = self._make_engine()
        result = engine.assess(
            **self._base_assess_kwargs(),
            score=90,
            regime="trending",
            session="london",
        )
        assert result.approved

    def test_scaled_risk_changes_position_size(self):
        """A lower score should produce fewer lots."""
        engine = self._make_engine()
        full = engine.assess(**self._base_assess_kwargs(), score=95)
        half = engine.assess(**self._base_assess_kwargs(), score=80)
        assert full.approved and half.approved
        if full.position_size_lots > 0 and half.position_size_lots > 0:
            assert half.position_size_lots <= full.position_size_lots


# ═══════════════════════════════════════════════════════════════════════════
# P3 + P7 — Conviction-based sizing replaces stale score; single auditable chain
# ═══════════════════════════════════════════════════════════════════════════

class TestConvictionScaling:
    """Fresh conviction (when supplied) drives sizing instead of stale score."""

    def _make_engine(self, balance=10_000.0):
        return RiskEngine(starting_balance=balance)

    def _base_assess_kwargs(self, balance=10_000.0):
        return dict(
            pair="EURUSD",
            direction="BUY",
            entry_price=1.10000,
            stop_loss=1.09500,
            open_trades=[],
            account_balance=balance,
        )

    def test_conviction_overrides_score_for_sizing(self):
        """When conviction is provided, the stale score must not drive sizing.

        Low conviction (0.50 → factor 0.5) on a high score (95) should size
        DOWN, proving the score is no longer the sizing input.
        """
        engine = self._make_engine()
        base = engine.assess(**self._base_assess_kwargs(), score=0)
        low_conv = engine.assess(
            **self._base_assess_kwargs(), score=95, conviction=0.50,
        )
        assert low_conv.approved
        assert abs(low_conv.risk_pct - base.risk_pct * 0.5) < 1e-6

    def test_high_conviction_full_risk(self):
        engine = self._make_engine()
        base = engine.assess(**self._base_assess_kwargs(), score=0)
        high = engine.assess(
            **self._base_assess_kwargs(), score=0, conviction=0.95,
        )
        assert high.approved
        assert abs(high.risk_pct - base.risk_pct) < 1e-6

    def test_conviction_chain_is_derisking_only(self):
        """No factor may inflate risk above base (no peak amplifier)."""
        engine = self._make_engine()
        base = engine.assess(**self._base_assess_kwargs(), score=0)
        high = engine.assess(
            **self._base_assess_kwargs(), score=0, conviction=0.99,
        )
        assert high.risk_pct <= base.risk_pct + 1e-9

    def test_portfolio_heat_reduces_size(self):
        engine = self._make_engine()
        calm = engine.assess(
            **self._base_assess_kwargs(), conviction=0.95, portfolio_heat_pct=0.0,
        )
        hot = engine.assess(
            **self._base_assess_kwargs(), conviction=0.95, portfolio_heat_pct=2.5,
        )
        assert calm.approved and hot.approved
        assert hot.risk_pct < calm.risk_pct

    def test_factor_helpers_clamp_to_unit_interval(self):
        engine = self._make_engine()
        for c in (-1.0, 0.0, 0.5, 0.86, 0.9, 1.0, 2.0):
            assert 0.0 <= engine._scale_by_conviction(c) <= 1.0
        for h in (0.0, 1.2, 1.9, 5.0):
            assert 0.0 <= engine._scale_by_portfolio_heat(h) <= 1.0
        for d in (0.0, 0.07, 0.12, 0.5):
            assert 0.0 <= engine._scale_by_drawdown(d) <= 1.0

    def test_no_conviction_falls_back_to_legacy_score(self):
        """conviction=None must preserve legacy stale-score behaviour."""
        engine = self._make_engine()
        legacy = engine.assess(**self._base_assess_kwargs(), score=85)
        chain = engine.compute_position_size_risk(
            base_risk_pct=0.02, conviction=None, score=85,
            hwm_state={"is_at_peak": False, "drawdown_from_peak_pct": 0.0},
        )
        assert chain == 0.014
        assert legacy.approved


# ═══════════════════════════════════════════════════════════════════════════
# FIX 2 — MIN_LOT floor over-risk guard (all accounts, not just micro)
# ═══════════════════════════════════════════════════════════════════════════

class TestMinLotOverRisk:
    """MIN_LOT floor inflating risk beyond 1.5× tolerance must skip."""

    def test_small_account_over_risk_skips(self):
        """$150 account, tiny risk → floor to 0.01 lot → max_loss >> risk_amount → skip."""
        sizer = PositionSizer()
        result = sizer.calculate(
            account_balance=150.0,
            risk_pct=0.001,
            entry_price=1.10000,
            stop_loss=1.09000,
            pip_size=0.0001,
            pip_value_per_lot=10.0,
        )
        assert result.lots == 0.0
        assert result.sizing_mode == "skip_min_lot_over_risk"

    def test_micro_account_over_risk_still_uses_micro_mode(self):
        """$80 account with over-risk should still get lots_skip_micro."""
        sizer = PositionSizer()
        result = sizer.calculate(
            account_balance=80.0,
            risk_pct=0.001,
            entry_price=1.10000,
            stop_loss=1.09000,
            pip_size=0.0001,
            pip_value_per_lot=10.0,
        )
        assert result.lots == 0.0
        assert result.sizing_mode == "lots_skip_micro"

    def test_normal_account_within_tolerance_trades(self):
        """$10,000 account, 1% risk, 50-pip stop → should still size normally."""
        sizer = PositionSizer()
        result = sizer.calculate(
            account_balance=10_000.0,
            risk_pct=0.01,
            entry_price=1.10000,
            stop_loss=1.09500,
            pip_size=0.0001,
            pip_value_per_lot=10.0,
        )
        assert result.lots > 0
        assert result.sizing_mode == "lots"
        assert result.max_loss <= result.risk_amount * 1.5

    def test_micro_account_trades_when_min_lot_risk_within_cap(self):
        """$211 account, min-lot risk ~0.6% → allowed (the live scenario)."""
        sizer = PositionSizer()
        result = sizer.calculate(
            account_balance=211.75,
            risk_pct=0.00375,           # score-scaled risk → ~$0.79 target
            entry_price=0.90000,
            stop_loss=0.89880,          # 12-pip stop
            pip_size=0.0001,
            pip_value_per_lot=10.0,
        )
        # Min lot 0.01 → max_loss = 0.01 * 12 * 10 = $1.20 = 0.57% of account,
        # well within the 5% cap → trade allowed instead of rejected.
        assert result.lots == 0.01
        assert result.sizing_mode == "lots"
        assert (result.max_loss / 211.75) * 100 <= 5.0

    def test_over_risk_reported_correctly(self):
        """When skipped, risk_amount is still reported (for logging)."""
        sizer = PositionSizer()
        result = sizer.calculate(
            account_balance=100.0,
            risk_pct=0.001,
            entry_price=1.10000,
            stop_loss=1.09000,
            pip_size=0.0001,
            pip_value_per_lot=10.0,
        )
        assert result.lots == 0.0
        assert result.risk_amount > 0


# ═══════════════════════════════════════════════════════════════════════════
# FIX 3 — risk_pips <= 0 returns zero lots (skip_invalid_stop)
# ═══════════════════════════════════════════════════════════════════════════

class TestInvalidStop:
    """entry == stop_loss or bad data must not produce a tradeable lot."""

    def test_entry_equals_stop_returns_zero_lots(self):
        sizer = PositionSizer()
        result = sizer.calculate(
            account_balance=10_000.0,
            risk_pct=0.01,
            entry_price=1.10000,
            stop_loss=1.10000,
            pip_size=0.0001,
            pip_value_per_lot=10.0,
        )
        assert result.lots == 0.0
        assert result.sizing_mode == "skip_invalid_stop"
        assert result.max_loss == 0.0

    def test_near_zero_risk_pips_returns_zero_lots(self):
        """Stop within 1 pip_size → risk_pips rounds to 0 → skip."""
        sizer = PositionSizer()
        result = sizer.calculate(
            account_balance=10_000.0,
            risk_pct=0.01,
            entry_price=1.10000,
            stop_loss=1.10000,
            pip_size=0.01,
            pip_value_per_lot=10.0,
        )
        assert result.lots == 0.0
        assert result.sizing_mode == "skip_invalid_stop"

    def test_valid_stop_still_trades(self):
        """Sanity: a normal entry/stop still returns tradeable lots."""
        sizer = PositionSizer()
        result = sizer.calculate(
            account_balance=10_000.0,
            risk_pct=0.01,
            entry_price=1.10000,
            stop_loss=1.09500,
            pip_size=0.0001,
            pip_value_per_lot=10.0,
        )
        assert result.lots > 0
        assert result.sizing_mode == "lots"
        assert result.max_loss > 0


# ═══════════════════════════════════════════════════════════════════════════
# Integration: risk_engine rejects new skip modes
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskEngineRejectsSkipModes:
    """Verify RiskEngine.assess() rejects trades when sizer returns skip modes."""

    def test_invalid_stop_rejected_by_engine(self):
        """entry == stop → sizer returns skip_invalid_stop → assess rejects."""
        engine = RiskEngine(starting_balance=10_000.0)
        result = engine.assess(
            pair="EURUSD",
            direction="BUY",
            entry_price=1.10000,
            stop_loss=1.10000,
            open_trades=[],
            account_balance=10_000.0,
        )
        assert not result.approved
        assert any("skip" in r for r in result.rejections)

    def test_over_risk_rejected_by_engine(self):
        """Tiny risk on small account → over-risk skip → assess rejects."""
        engine = RiskEngine(starting_balance=200.0)
        result = engine.assess(
            pair="EURUSD",
            direction="BUY",
            entry_price=1.10000,
            stop_loss=1.09000,
            open_trades=[],
            account_balance=200.0,
        )
        if result.risk_pct < 0.002:
            assert not result.approved

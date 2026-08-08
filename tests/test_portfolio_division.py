"""
APEX TRADER — Portfolio Division tests.

Covers the cohesive sizing layer that replaced the inline fresh-PositionSizer +
ad-hoc multiplier chain:

  1. Neutral factors → positive size, FULL exposure.
  2. Factor fold de-risks and clamps (≤1.0 ceiling, ≥0.15 floor).
  3. Daily-loss budget reduces the size to fit remaining room (REDUCED).
  4. Daily-loss budget rejects when no room / min-lot floor breaches it.
  5. Non-positive balance / zero base risk are rejected.
  6. Deriv stake path is sized as stake, not lots.
  7. End-to-end with the real PositionSizer.
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

# ── Stub heavy deps before any app imports ────────────────────────────────
for _mod in ("MetaTrader5", "requests", "websockets", "aiosqlite"):
    sys.modules.setdefault(_mod, MagicMock())
if "loguru" not in sys.modules:
    _loguru = MagicMock()
    _loguru.logger = MagicMock()
    sys.modules["loguru"] = _loguru

from portfolio.division import PortfolioDivision  # noqa: E402
from portfolio.models import (  # noqa: E402
    PortfolioAccount,
    PortfolioCandidate,
    SizingFactors,
)
from portfolio.verdict import (  # noqa: E402
    EXPOSURE_FULL,
    EXPOSURE_REDUCED,
)


class FakeSizeResult:
    """Mirrors risk.position_sizer.SizeResult's read surface."""

    def __init__(self, lots=0.0, stake_usd=0.0, risk_pips=0.0, pip_value=0.0,
                 max_loss=0.0, sizing_mode="lots"):
        self.lots = lots
        self.stake_usd = stake_usd
        self.risk_pips = risk_pips
        self.pip_value = pip_value
        self.max_loss = max_loss
        self.sizing_mode = sizing_mode


class FakeSizer:
    """Deterministic sizer: lots scale linearly with risk_pct.

    1.0 lot per full ``unit_risk`` fraction at a fixed 10 pips × $10/pip so
    max_loss == lots * 100. Lets tests reason about budget math exactly.
    """

    def __init__(self, unit_risk=0.01):
        self.unit_risk = unit_risk

    def calculate(self, *, account_balance, risk_pct, entry_price, stop_loss,
                  pip_size, pip_value_per_lot, context=None, symbol=""):
        if context is not None and getattr(context, "uses_stake", False):
            stake = round(account_balance * risk_pct, 2)
            mode = "stake" if stake > 0 else "skip_zero"
            return FakeSizeResult(stake_usd=stake, max_loss=stake, sizing_mode=mode)
        if risk_pct <= 0:
            return FakeSizeResult(sizing_mode="skip_zero_alloc")
        lots = round(risk_pct / self.unit_risk, 2)
        return FakeSizeResult(
            lots=lots, risk_pips=10.0, pip_value=10.0,
            max_loss=lots * 100.0, sizing_mode="lots",
        )

    def adjust_for_volatility(self, base, cur, avg):
        return base


def _candidate(uses_stake=False):
    ctx = SimpleNamespace(uses_stake=uses_stake)
    return PortfolioCandidate(
        symbol="EURUSD", direction="LONG", entry_price=1.10,
        stop_loss=1.0990, conviction=80.0, context=ctx,
        pip_size=0.0001, pip_value_per_lot=10.0,
    )


def _account(balance=10_000.0, daily_pnl=0.0, cap=0.0):
    return PortfolioAccount(
        balance=balance, account_key="mt5:1",
        daily_pnl=daily_pnl, daily_loss_cap_pct=cap,
    )


def test_neutral_factors_full_size():
    pf = PortfolioDivision(FakeSizer())
    v = pf.evaluate(_candidate(), [], _account(),
                    SizingFactors(base_risk_pct=0.01))
    assert v.approved
    assert v.sizing_mode == "lots"
    assert v.approved_size == 1.0      # 0.01 / 0.01 unit_risk
    assert abs(v.combined_mult - 1.0) < 1e-9
    assert v.exposure_verdict == EXPOSURE_FULL


def test_factor_fold_derisks_and_clamps_ceiling():
    pf = PortfolioDivision(FakeSizer())
    # Two >1 factors must NOT inflate above the 1.0 ceiling.
    v = pf.evaluate(_candidate(), [], _account(),
                    SizingFactors(base_risk_pct=0.01, de_size_mult=1.5,
                                  orch_mult=1.5))
    assert v.approved
    assert v.combined_mult == 1.0
    assert v.approved_size == 1.0

    # A 0.5 factor halves the size.
    v2 = pf.evaluate(_candidate(), [], _account(),
                     SizingFactors(base_risk_pct=0.01, adapt_mult=0.5))
    assert v2.combined_mult == 0.5
    assert v2.approved_size == 0.5


def test_factor_fold_floor():
    pf = PortfolioDivision(FakeSizer())
    # Product 0.01 must clamp up to the 0.15 floor.
    v = pf.evaluate(_candidate(), [], _account(),
                    SizingFactors(base_risk_pct=0.01, adapt_mult=0.1,
                                  vol_mult=0.1))
    assert v.combined_mult == 0.15


def test_daily_budget_reduces_size():
    pf = PortfolioDivision(FakeSizer())
    # base risk 0.02 → 2.0 lots → max_loss $200. Remaining budget = 3% of
    # $10k = $300 + (-$200 daily loss) = $100 → must reduce to ≤ $100.
    v = pf.evaluate(_candidate(), [],
                    _account(daily_pnl=-200.0, cap=3.0),
                    SizingFactors(base_risk_pct=0.02))
    assert v.approved
    assert v.exposure_verdict == EXPOSURE_REDUCED
    assert v.max_loss <= 100.0 + 1e-6
    assert v.approved_size < 2.0


def test_daily_budget_no_room_rejects():
    pf = PortfolioDivision(FakeSizer())
    # Daily loss already exceeds the cap → no remaining room → reject.
    v = pf.evaluate(_candidate(), [],
                    _account(daily_pnl=-400.0, cap=3.0),
                    SizingFactors(base_risk_pct=0.02))
    assert not v.approved
    assert "daily" in v.reason.lower()


def test_min_lot_floor_breaches_budget_rejects():
    # Sizer that always returns the broker minimum 0.01 lot (max_loss $1) but
    # remaining budget is only $0.50 → even the min lot can't fit → reject.
    class MinLotSizer(FakeSizer):
        def calculate(self, **kw):
            return FakeSizeResult(lots=0.01, risk_pips=10.0, pip_value=10.0,
                                  max_loss=1.0, sizing_mode="lots")

    pf = PortfolioDivision(MinLotSizer())
    v = pf.evaluate(_candidate(), [],
                    _account(balance=100.0, daily_pnl=-2.5, cap=3.0),
                    SizingFactors(base_risk_pct=0.02))
    # remaining = 3% * 100 - 2.5 = 0.5 ; min-lot max_loss 1.0 > 0.5 → reject
    assert not v.approved


def test_non_positive_balance_rejected():
    pf = PortfolioDivision(FakeSizer())
    v = pf.evaluate(_candidate(), [], _account(balance=0.0),
                    SizingFactors(base_risk_pct=0.01))
    assert not v.approved
    assert "balance" in v.reason.lower()


def test_zero_base_risk_rejected():
    pf = PortfolioDivision(FakeSizer())
    v = pf.evaluate(_candidate(), [], _account(),
                    SizingFactors(base_risk_pct=0.0))
    assert not v.approved


def test_stake_path():
    pf = PortfolioDivision(FakeSizer())
    v = pf.evaluate(_candidate(uses_stake=True), [], _account(),
                    SizingFactors(base_risk_pct=0.01, adapt_mult=0.5))
    assert v.approved
    assert v.sizing_mode == "stake"
    # $10k * 1% = $100 stake, halved by the 0.5 factor → $50.
    assert v.stake_usd == 50.0
    assert v.lots == 0.0


def test_pair_concentration_soft_budget():
    pf = PortfolioDivision(FakeSizer(), max_pair_concentration=1)
    book = [SimpleNamespace(symbol="EURUSD", direction="LONG", broker="mt5")]
    v = pf.evaluate(_candidate(), book, _account(),
                    SizingFactors(base_risk_pct=0.01))
    assert v.approved
    assert v.exposure_verdict == EXPOSURE_REDUCED
    assert v.portfolio_impact.get("pair_count") == 2


def test_end_to_end_real_sizer():
    try:
        from risk.position_sizer import PositionSizer
    except ModuleNotFoundError as exc:  # sandbox without numpy/config deps
        try:
            import pytest
            pytest.skip(f"sizer deps unavailable: {exc}")
        except ImportError:
            print("  (skipped test_end_to_end_real_sizer: deps unavailable)")
            return

    pf = PortfolioDivision(PositionSizer())
    v = pf.evaluate(_candidate(), [], _account(balance=10_000.0),
                    SizingFactors(base_risk_pct=0.01))
    assert v.approved
    assert v.sizing_mode == "lots"
    assert v.approved_size > 0
    assert v.max_loss > 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ✓ {fn.__name__}")
    print(f"{passed}/{len(fns)} portfolio division tests passed")

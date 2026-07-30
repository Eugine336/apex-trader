"""Backtest ↔ live parity — sizing / optimization subsystems (Phase 2).

Verifies that ``brain.backtest_engine`` threads the SAME graded-sizing chain the
live event-driven plane runs between the RiskGovernor verdict and
``PortfolioDivision.evaluate`` — the Orchestrator round table, the adaptive
optimizer (losing-pattern / AVOID vetoes + learned size), the system-volatility,
opportunity-density and execution-quality monitors, the capital allocator and
the opportunity-quality sizer — folding every multiplier into one
:class:`SizingFactors` bundle, and that ``legacy_mode`` is completely unaffected
(the subsystems stay ``None`` and none of the new chain runs).
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from brain.backtest_engine import BacktestEngine
from portfolio.models import SizingFactors


_NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _wm_stub():
    """Minimal WorldModel surface the sizing chain reads."""
    return SimpleNamespace(
        structure_by_tf=lambda: {},
        regime_by_tf=lambda: {"H1": "TREND"},
        candidates_list=lambda: [],
    )


def _signal():
    return SimpleNamespace(entry_price=1.1000, stop_loss=1.0950, tp2=1.1150)


def _zone(zone_type="FVG_OB_OVERLAP"):
    return SimpleNamespace(zone_type=zone_type)


def _de(conviction=0.8):
    return SimpleNamespace(conviction=conviction)


# ── Construction ─────────────────────────────────────────────────────────


def test_non_legacy_wires_sizing_subsystems():
    eng = BacktestEngine()
    assert eng.legacy_mode is False
    assert eng.bt_orchestrator is not None
    assert eng.bt_opportunity_density is not None
    assert eng.bt_execution_monitor is not None
    assert eng.bt_capital_allocator is not None
    assert eng.bt_oq_sizer is not None
    # ml_adapter is best-effort (may be None on a cold environment); the
    # attribute must always exist so the gate degrades cleanly.
    assert hasattr(eng, "bt_ml_adapter")


def test_legacy_mode_leaves_sizing_subsystems_none():
    eng = BacktestEngine(legacy_mode=True)
    assert eng.legacy_mode is True
    assert eng.bt_orchestrator is None
    assert eng.bt_ml_adapter is None
    assert eng.bt_opportunity_density is None
    assert eng.bt_execution_monitor is None
    assert eng.bt_capital_allocator is None
    assert eng.bt_oq_sizer is None


# ── Orchestrator round table ──────────────────────────────────────────────


def test_orchestrator_mult_failsafe_when_absent():
    eng = BacktestEngine()
    eng.bt_orchestrator = None
    assert eng._orchestrator_size_mult(
        "EURUSD", "LONG", _wm_stub(), 90.0, 0.8,
    ) == (1.0, False)


def test_orchestrator_mult_vetoes_entry():
    eng = BacktestEngine()
    eng.bt_orchestrator = SimpleNamespace(
        evaluate=lambda proposal: SimpleNamespace(
            vetoed=True, veto_reason="physics", size_multiplier=0.0,
        )
    )
    mult, vetoed = eng._orchestrator_size_mult("EURUSD", "LONG", _wm_stub(), 90.0, 0.8)
    assert vetoed is True
    assert mult == 0.0


def test_orchestrator_mult_grades_size():
    eng = BacktestEngine()
    eng.bt_orchestrator = SimpleNamespace(
        evaluate=lambda proposal: SimpleNamespace(
            vetoed=False, veto_reason="", size_multiplier=0.6,
        )
    )
    mult, vetoed = eng._orchestrator_size_mult("EURUSD", "LONG", _wm_stub(), 90.0, 0.8)
    assert vetoed is False
    assert mult == pytest.approx(0.6)


# ── Adaptive optimizer ─────────────────────────────────────────────────────


def test_adaptive_mult_failsafe_when_absent():
    eng = BacktestEngine()
    eng.bt_ml_adapter = None
    assert eng._adaptive_size_mult("EURUSD", _wm_stub(), _NOW, "FVG") == (1.0, False)


def test_adaptive_mult_blocks_losing_pattern():
    eng = BacktestEngine()
    eng.bt_ml_adapter = SimpleNamespace(
        is_losing_pattern=lambda *a, **k: (True, "confirmed loser"),
        get_trade_adjustments=lambda *a, **k: SimpleNamespace(
            should_trade=True, position_size_multiplier=1.0, reason="",
        ),
    )
    _, blocked = eng._adaptive_size_mult("EURUSD", _wm_stub(), _NOW, "FVG")
    assert blocked is True


def test_adaptive_mult_blocks_avoid_veto():
    eng = BacktestEngine()
    eng.bt_ml_adapter = SimpleNamespace(
        is_losing_pattern=lambda *a, **k: (False, ""),
        get_trade_adjustments=lambda *a, **k: SimpleNamespace(
            should_trade=False, position_size_multiplier=1.0, reason="avoid",
        ),
    )
    _, blocked = eng._adaptive_size_mult("EURUSD", _wm_stub(), _NOW, "FVG")
    assert blocked is True


def test_adaptive_mult_returns_learned_size():
    eng = BacktestEngine()
    eng.bt_ml_adapter = SimpleNamespace(
        is_losing_pattern=lambda *a, **k: (False, ""),
        get_trade_adjustments=lambda *a, **k: SimpleNamespace(
            should_trade=True, position_size_multiplier=0.75, reason="",
        ),
    )
    mult, blocked = eng._adaptive_size_mult("EURUSD", _wm_stub(), _NOW, "FVG")
    assert blocked is False
    assert mult == pytest.approx(0.75)


# ── Pure multiplier helpers ────────────────────────────────────────────────


def test_capital_alloc_mult_cold_is_neutral():
    eng = BacktestEngine()
    # Isolated in-memory allocator with no measured history → neutral 1.0.
    assert eng._capital_alloc_mult(_wm_stub()) == 1.0


def test_instrument_vol_mult_neutral_without_slice():
    eng = BacktestEngine()
    assert eng._instrument_vol_mult({}) == 1.0


def test_instrument_vol_mult_computes_from_atr():
    eng = BacktestEngine()
    n = 60
    highs = [1.10 + (0.0005 if i < 40 else 0.0020) for i in range(n)]
    df = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC"),
        "open": [1.10] * n, "high": highs, "low": [1.10] * n, "close": [1.10] * n,
    })
    m = eng._instrument_vol_mult({"M5": df})
    assert m > 0.0


def test_opportunity_quality_mult_identity_when_disabled():
    eng = BacktestEngine()
    eng.bt_oq_sizer = SimpleNamespace(enabled=False)
    assert eng._opportunity_quality_mult(_wm_stub(), "LONG", 90.0) == 1.0
    eng.bt_oq_sizer = None
    assert eng._opportunity_quality_mult(_wm_stub(), "LONG", 90.0) == 1.0


# ── SizingFactors composition ──────────────────────────────────────────────


def _neutralize(eng):
    """Silence every optional subsystem so a test controls one factor at a time."""
    eng.bt_orchestrator = None
    eng.bt_ml_adapter = None
    eng.system_volatility_monitor = None
    eng.bt_opportunity_density = None
    eng.bt_execution_monitor = None
    eng.bt_capital_allocator = None
    eng.bt_oq_sizer = None


def test_build_sizing_factors_composes_multipliers():
    eng = BacktestEngine()
    eng.bt_orchestrator = SimpleNamespace(
        evaluate=lambda p: SimpleNamespace(
            vetoed=False, veto_reason="", size_multiplier=0.9,
        )
    )
    eng.bt_ml_adapter = SimpleNamespace(
        is_losing_pattern=lambda *a, **k: (False, ""),
        get_trade_adjustments=lambda *a, **k: SimpleNamespace(
            should_trade=True, position_size_multiplier=0.8, reason="",
        ),
    )
    eng.system_volatility_monitor = SimpleNamespace(get_size_multiplier=lambda: 0.7)
    eng.bt_opportunity_density = SimpleNamespace(get_size_multiplier=lambda: 0.95)
    eng.bt_execution_monitor = SimpleNamespace(get_size_multiplier=lambda s: 0.85)
    eng.bt_capital_allocator = None
    eng.bt_oq_sizer = None

    factors = eng._build_sizing_factors(
        "EURUSD", "LONG", _wm_stub(), {}, _signal(), _zone(), 90,
        _de(), 1.0, 0.02, _NOW,
    )
    assert factors is not None
    assert isinstance(factors, SizingFactors)
    assert factors.base_risk_pct == pytest.approx(0.02)
    assert factors.de_size_mult == pytest.approx(1.0)
    assert factors.orch_mult == pytest.approx(0.9)
    assert factors.adapt_mult == pytest.approx(0.8)
    assert factors.vol_mult == pytest.approx(0.7)
    assert factors.density_mult == pytest.approx(0.95)
    assert factors.exec_mult == pytest.approx(0.85)
    assert factors.cap_mult == pytest.approx(1.0)
    assert factors.inst_vol_mult == pytest.approx(1.0)


def test_build_sizing_factors_returns_none_on_orchestrator_veto():
    eng = BacktestEngine()
    _neutralize(eng)
    eng.bt_orchestrator = SimpleNamespace(
        evaluate=lambda p: SimpleNamespace(
            vetoed=True, veto_reason="physics", size_multiplier=0.0,
        )
    )
    assert eng._build_sizing_factors(
        "EURUSD", "LONG", _wm_stub(), {}, _signal(), _zone(), 90,
        _de(), 1.0, 0.02, _NOW,
    ) is None


def test_build_sizing_factors_returns_none_on_adaptive_block():
    eng = BacktestEngine()
    _neutralize(eng)
    eng.bt_ml_adapter = SimpleNamespace(
        is_losing_pattern=lambda *a, **k: (True, "loser"),
        get_trade_adjustments=lambda *a, **k: SimpleNamespace(
            should_trade=True, position_size_multiplier=1.0, reason="",
        ),
    )
    assert eng._build_sizing_factors(
        "EURUSD", "LONG", _wm_stub(), {}, _signal(), _zone(), 90,
        _de(), 1.0, 0.02, _NOW,
    ) is None


def test_build_sizing_factors_oq_folds_into_base_risk():
    eng = BacktestEngine()
    _neutralize(eng)
    # A boost > 1.0 lifts the base risk directly (live parity: risk_pct *= q),
    # unlike the de-risking factors PortfolioDivision clamps to <= 1.0.
    eng.bt_oq_sizer = SimpleNamespace(enabled=True, multiplier=lambda **k: 1.2)
    factors = eng._build_sizing_factors(
        "EURUSD", "LONG", _wm_stub(), {}, _signal(), _zone(), 90,
        _de(), 1.0, 0.02, _NOW,
    )
    assert factors is not None
    assert factors.base_risk_pct == pytest.approx(0.024)


# ── PortfolioDivision folds the factor product ─────────────────────────────


def test_size_trade_applies_factor_multipliers():
    eng = BacktestEngine()
    signal = _signal()
    full = eng._size_trade(
        "EURUSD", "LONG", signal,
        SizingFactors(base_risk_pct=eng.risk_per_trade, de_size_mult=1.0),
        0.8, 10_000.0,
    )
    half = eng._size_trade(
        "EURUSD", "LONG", signal,
        SizingFactors(base_risk_pct=eng.risk_per_trade, de_size_mult=0.5),
        0.8, 10_000.0,
    )
    assert full is not None and half is not None
    assert half[0] < full[0]  # a 0.5 de-risking factor sizes down


# ── Adaptive close feed ────────────────────────────────────────────────────


def test_register_close_adaptive_none_safe():
    eng = BacktestEngine()
    eng.bt_ml_adapter = None
    # Must not raise when the adapter is degraded to None.
    eng._register_close_adaptive({"outcome": "WIN"})


def test_register_close_adaptive_advances_optimizer():
    eng = BacktestEngine()
    seen = []
    eng.bt_ml_adapter = SimpleNamespace(
        register_new_trade=lambda exit_cause=None: seen.append(exit_cause),
    )
    eng._register_close_adaptive({"exit_reason": "tp2", "outcome": "WIN"})
    assert seen == ["tp2"]


# ── Reset per run (no state leak between backtests) ───────────────────────


def test_run_rebuilds_sizing_subsystems():
    eng = BacktestEngine(starting_balance=200.0)
    # Simulate a torn-down / leaked state from a prior run.
    eng.bt_orchestrator = None
    eng.bt_oq_sizer = None

    t = pd.date_range("2024-01-01", periods=200, freq="min")
    flat = pd.DataFrame({
        "time": t, "open": 1.10, "high": 1.1002, "low": 1.0998, "close": 1.10,
    })
    data = {tf: flat.copy() for tf in ("M1", "M5", "M15", "H1", "H4")}
    eng.run("EURUSD", data, start_index=120, end_index=199)

    # The run reset rebuilds the sizing subsystems fresh.
    assert eng.bt_orchestrator is not None
    assert eng.bt_oq_sizer is not None

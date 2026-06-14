"""
Tests for Tier 2 #14 — regime-adaptive trade shaping inside the planner.

Verifies that the RegimeLearner's learned values are applied with the correct
units and bounded clamps, and that the planner is NEUTRAL (unchanged) when no
regime adjustment is supplied.

These call the planner's `_sl_plan` / `_tp_plan` directly to isolate the
shaping logic from the full ENTER/SKIP/WAIT pipeline.
"""

import pytest

from planning.models import TradePlanContext
from planning.trade_planner import PlannerConfig, TradePlanner

_CFG = PlannerConfig()
_PLANNER = TradePlanner(_CFG)
_ATR = 20.0


def _ctx(**kw) -> TradePlanContext:
    base = dict(
        symbol="EURUSD",
        direction="LONG",
        current_price=1.1000,
        zone_entry_price=1.1000,
        pip_size=0.0001,
        atr_pips=_ATR,
        structure_sl_available=False,  # force the ATR-stop branch
        de_tf_alignment=0.5,           # below trail-only threshold
        rl_expected_r=0.0,             # skip the fixed-TP branch → partial_trail
    )
    base.update(kw)
    return TradePlanContext(**base)


# ── SL buffer (applied pre-sizing, ATR stop only, bounded) ────────────────

class TestRegimeSLBuffer:
    def test_neutral_when_no_buffer(self):
        _, _, sl_pips = _PLANNER._sl_plan(_ctx())
        assert sl_pips == pytest.approx(_CFG.default_sl_atr_multiplier * _ATR)

    def test_buffer_applied(self):
        _, _, sl_pips = _PLANNER._sl_plan(_ctx(regime_sl_buffer_pips=3.0))
        assert sl_pips == pytest.approx(_CFG.default_sl_atr_multiplier * _ATR + 3.0)

    def test_buffer_clamped_high(self):
        _, _, sl_pips = _PLANNER._sl_plan(_ctx(regime_sl_buffer_pips=99.0))
        assert sl_pips == pytest.approx(_CFG.default_sl_atr_multiplier * _ATR + 5.0)

    def test_buffer_clamped_low(self):
        _, _, sl_pips = _PLANNER._sl_plan(_ctx(regime_sl_buffer_pips=-99.0))
        assert sl_pips == pytest.approx(_CFG.default_sl_atr_multiplier * _ATR - 2.0)

    def test_structure_stop_unaffected_by_buffer(self):
        # A structure stop is a real level — the regime buffer must NOT pad it.
        ctx = _ctx(
            structure_sl_available=True,
            proposed_sl_price=1.0980,
            proposed_sl_pips=20.0,
            regime_sl_buffer_pips=5.0,
        )
        strategy, _, sl_pips = _PLANNER._sl_plan(ctx)
        assert strategy == "structure"
        assert sl_pips == pytest.approx(20.0)


# ── TP multiplier + runner fraction (scales RR only, bounded) ─────────────

class TestRegimeTPShaping:
    def test_neutral_tp_and_runner(self):
        strat, _, tp1_rr, _, tp2_rr, runner = _PLANNER._tp_plan(_ctx(), sl_pips=20.0)
        assert strat == "partial_trail"
        assert tp1_rr == pytest.approx(_CFG.default_tp1_rr)
        assert tp2_rr == pytest.approx(_CFG.default_tp2_rr)
        assert runner == pytest.approx(_CFG.default_runner_pct)

    def test_tp_mult_scales_rr(self):
        _, _, tp1_rr, _, tp2_rr, _ = _PLANNER._tp_plan(
            _ctx(regime_tp_mult=1.5), sl_pips=20.0,
        )
        assert tp1_rr == pytest.approx(_CFG.default_tp1_rr * 1.5)
        assert tp2_rr == pytest.approx(_CFG.default_tp2_rr * 1.5)

    def test_tp_mult_clamped(self):
        _, _, tp1_rr, _, _, _ = _PLANNER._tp_plan(
            _ctx(regime_tp_mult=5.0), sl_pips=20.0,
        )
        assert tp1_rr == pytest.approx(_CFG.default_tp1_rr * 1.5)  # clamped to 1.5

    def test_runner_override_and_clamp(self):
        *_, runner = _PLANNER._tp_plan(_ctx(regime_runner_pct=0.6), sl_pips=20.0)
        assert runner == pytest.approx(0.6)
        *_, runner_hi = _PLANNER._tp_plan(_ctx(regime_runner_pct=0.95), sl_pips=20.0)
        assert runner_hi == pytest.approx(0.7)  # clamped to 0.7

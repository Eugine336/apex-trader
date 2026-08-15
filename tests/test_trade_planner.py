"""Tests for the Trade Planner system (planner, calibrator, outcome logger)."""

import os
import tempfile

import pytest

from planning import (
    Calibrator,
    OutcomeLogger,
    PlannerConfig,
    TradePlanContext,
    TradePlanner,
)
from planning.models import TradePlan


# ── Context builders ─────────────────────────────────────────────────────

def _strong_long(**overrides) -> TradePlanContext:
    base = dict(
        symbol="EURUSD",
        pip_size=0.0001,
        atr_pips=20.0,
        current_price=1.1000,
        direction="LONG",
        scanner_score=82.0,
        zone_type="ORDER_BLOCK",
        zone_quality=0.85,
        zone_entry_price=1.0998,
        de_confidence=0.75,
        de_tf_alignment=0.80,
        rl_action=1,
        rl_confidence=0.6,
        rl_expected_r=2.2,
        pair_win_rate=0.6,
        session_win_rate=0.6,
        proposed_sl_price=1.0980,
        proposed_sl_pips=20.0,
        structure_sl_available=True,
        risk_reward_2=3.0,
        base_risk_pct=0.5,
        session="LONDON",
        situation_label="TREND_CONTINUATION",
    )
    base.update(overrides)
    return TradePlanContext(**base)


# ── Planner: deprecated, no directional authority (V-005) ────────────────

def test_planner_instantiation_warns_deprecated():
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        TradePlanner()
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_plan_trade_is_non_directional_noop():
    # DIRECTIONAL_AUTHORITY (V-005): the planner emits no BUY/SELL verdict and
    # computes no advisor agreement — it returns a non-actionable, directionless
    # plan regardless of how "strong" the (upstream) setup looks.
    plan = TradePlanner().plan_trade(_strong_long())
    assert plan.action == "SKIP"
    assert plan.direction == ""
    assert plan.advisor_agreement == 0.0
    assert not plan.should_enter


def test_weighted_advisor_agreement_machinery_removed():
    # The forbidden weighted directional-agreement/consensus methods are gone.
    planner = TradePlanner()
    assert not hasattr(planner, "_advisor_agreement")
    assert not hasattr(planner, "advisor_vector")
    assert not hasattr(planner, "_gate_agreement")


# ── Execution-geometry helpers (given an already-decided direction) ───────
# These shape SL/TP/entry/size/BE for a direction decided UPSTREAM; they carry
# no directional authority of their own.

def test_wait_helper_when_weak_session_and_change_imminent():
    ctx = _strong_long(session_win_rate=0.30, minutes_to_session_change=20)
    reason, minutes = TradePlanner()._wait_decision(ctx)
    assert reason is not None
    assert minutes and minutes > 0


def test_entry_mode_limit_when_price_far_from_zone():
    # price 30 pips above zone, ATR 20 → > 0.5×ATR distance → LIMIT
    ctx = _strong_long(
        brain_entry_mode="PENDING",
        current_price=1.1030,
        zone_entry_price=1.1000,
        atr_pips=20.0,
    )
    mode, price = TradePlanner()._entry_mode(ctx)
    assert mode == "LIMIT"
    assert price == pytest.approx(1.1000)


def test_trail_only_for_strong_trend():
    ctx = _strong_long(de_tf_alignment=0.85)
    strat, _tp1, _tp1_rr, tp2, _tp2_rr, _runner = TradePlanner()._tp_plan(ctx, sl_pips=20.0)
    assert strat == "trail_only"
    assert tp2 is None


def test_fixed_tp_for_low_expected_r():
    ctx = _strong_long(de_tf_alignment=0.5, rl_expected_r=1.0)
    strat, _tp1, _tp1_rr, _tp2, _tp2_rr, runner = TradePlanner()._tp_plan(ctx, sl_pips=20.0)
    assert strat == "fixed_rr"
    assert runner == 0.0


def test_correlation_reduces_size():
    planner = TradePlanner()
    base_risk, _ = planner._size_plan(_strong_long(correlated_exposure=0.0))
    corr_risk, _ = planner._size_plan(_strong_long(correlated_exposure=0.8))
    assert corr_risk < base_risk


def test_news_tightens_breakeven_trigger():
    cfg = PlannerConfig()
    trigger = TradePlanner(cfg)._be_trigger(_strong_long(is_news_window=True, minutes_to_news=5))
    assert trigger == cfg.news_be_trigger_r


# ── Config round-trip ────────────────────────────────────────────────────

def test_planner_config_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cfg.json")
        cfg = PlannerConfig(min_confidence_to_enter=0.37, max_risk_pct=1.8)
        cfg.save(path)
        loaded = PlannerConfig.load(path)
        assert loaded.min_confidence_to_enter == pytest.approx(0.37)
        assert loaded.max_risk_pct == pytest.approx(1.8)
        assert loaded.enabled == cfg.enabled


def test_planner_config_load_missing_returns_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        loaded = PlannerConfig.load(os.path.join(tmp, "does_not_exist.json"))
        assert loaded.enabled is True


# ── Outcome logger ───────────────────────────────────────────────────────

def test_outcome_logger_joins_plan_and_outcome():
    with tempfile.TemporaryDirectory() as tmp:
        ol = OutcomeLogger(os.path.join(tmp, "j.jsonl"))
        plan = TradePlan(action="ENTER", direction="BUY", sl_pips=20)
        ol.log_plan(plan, TradePlanContext(symbol="EURUSD", direction="LONG"))
        # no outcome yet → not completed
        assert ol.completed_count() == 0
        ol.log_outcome(plan.plan_id, {"pnl_r": 1.5, "outcome": "TP2"})
        completed = ol.get_completed_trades()
        assert len(completed) == 1
        assert completed[0]["outcome"]["pnl_r"] == 1.5
        assert completed[0]["plan"]["sl_pips"] == 20


# ── Calibrator ───────────────────────────────────────────────────────────

def _make_completed(n, sl_strategy, win_rate, sl_pips=20.0):
    out = []
    wins = int(n * win_rate)
    for i in range(n):
        r = 2.0 if i < wins else -1.0
        out.append(
            {
                "plan_id": f"{sl_strategy}-{i}",
                "plan": {
                    "sl_strategy": sl_strategy,
                    "entry_mode": "MARKET",
                    "be_trigger_r": 0.5,
                    "confidence": 0.6,
                    "sl_pips": sl_pips,
                },
                "context": {},
                "outcome": {"pnl_r": r, "outcome": "TP2" if r > 0 else "SL"},
            }
        )
    return out


def test_calibrator_prefers_winning_sl_strategy():
    cfg = PlannerConfig(calibration_min_trades=20, calibration_interval_trades=10)
    cal = Calibrator(cfg)
    trades = _make_completed(30, "structure", 0.7) + _make_completed(30, "atr", 0.4)
    before = cfg.prefer_structure_sl_within_atr
    new = cal.calibrate(trades)
    # structure wins more → widen its selection band
    assert new.prefer_structure_sl_within_atr > before


def test_calibrator_clamps_adjustment():
    cfg = PlannerConfig(calibration_max_adjustment_pct=0.20)
    cal = Calibrator(cfg)
    trades = _make_completed(50, "structure", 0.95) + _make_completed(50, "atr", 0.05)
    before = cfg.prefer_structure_sl_within_atr
    new = cal.calibrate(trades)
    # never more than +20% in a single cycle
    assert new.prefer_structure_sl_within_atr <= before * 1.20 + 1e-9


def test_should_calibrate_respects_thresholds():
    cfg = PlannerConfig(
        calibration_enabled=True,
        calibration_min_trades=50,
        calibration_interval_trades=25,
    )
    cal = Calibrator(cfg)
    assert cal.should_calibrate(40) is False        # below min
    assert cal.should_calibrate(60) is True         # above min + first run
    cal._last_calibration_count = 60
    assert cal.should_calibrate(70) is False         # < interval since last
    assert cal.should_calibrate(85) is True          # ≥ interval since last


def test_calibration_disabled_never_runs():
    cfg = PlannerConfig(calibration_enabled=False, calibration_min_trades=1)
    cal = Calibrator(cfg)
    assert cal.should_calibrate(1000) is False

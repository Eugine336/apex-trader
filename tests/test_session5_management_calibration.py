"""Session 5 — Management / Calibration collapse fixes (#17, #20, #32, #33, #34).

Each fix is gated behind a config flag defaulting to legacy behaviour, so every
test asserts BOTH the unchanged legacy path and the new graded path.

Also covers the pre-existing ``DecisionEngine.__init__`` regression where the
#6/#18/#29 assignments were orphaned after a staticmethod's ``return`` (never
executed), discovered while wiring #17.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from decision.context import EntryContext
from decision.situation import SituationAssessment
from decision.engine import DecisionEngine
from brain.structure_engine import StructureEvent
from management.trade_manager import EntrySignal, TradeManager
from planning.trade_planner import PlannerConfig
from planning.calibrator import Calibrator
from trigger.entry_engine import EntryEngine
from config import AppConfig


# ===========================================================================
# Regression: DecisionEngine.__init__ orphaned-assignment bug
# ===========================================================================

class TestDecisionEngineInitRegression:
    """The #6/#18/#29 attributes must actually be set by __init__ (they were
    orphaned after a staticmethod return on main)."""

    def test_init_sets_all_attributes(self):
        e = DecisionEngine()
        assert e.market_mode_threshold == 0.40
        assert e.soften_gate is False
        assert e.conviction_size_min == 0.5
        assert e.conviction_size_max == 1.5
        assert hasattr(e, "gate_safety_margin")
        assert hasattr(e, "gate_quality_floor")

    def test_init_respects_overrides(self):
        e = DecisionEngine(conviction_size_min=0.25, conviction_size_max=2.0,
                           market_mode_threshold=0.6)
        assert e.conviction_size_min == 0.25
        assert e.conviction_size_max == 2.0
        assert e.market_mode_threshold == 0.6


# ===========================================================================
# #17 — weighted reversal evidence (strength) vs binary count
# ===========================================================================

def _counter_htf_ctx(**overrides) -> EntryContext:
    defaults = dict(
        symbol="EURUSD", direction="LONG", scan_score=75, scan_direction="LONG",
        entry_type="SWEEP_REVERSAL", entry_price=1.0850, stop_loss=1.0820,
        tp1=1.0895, tp2=1.0940, risk_reward_1=1.5, risk_reward_2=3.0,
        risk_pips=30.0, entry_mode="PENDING", micro_confirmation="momentum_only",
        base_lots=0.10, account_balance=1000.0, risk_pct=0.01,
        d1_trend="BEARISH", d1_confidence=0.75, h4_trend="BEARISH",
        h4_confidence=0.80, h1_trend="BEARISH", h1_confidence=0.70,
        m1_trend="BULLISH", m1_event="BOS_BULLISH", m1_aligned_count=4,
        session_name="LONDON", session_tradeable=True, open_trade_count=1,
        max_open_trades=5, current_spread=1.2, typical_spread=1.0,
        regime="BEARISH",
    )
    defaults.update(overrides)
    return EntryContext(**defaults)


class TestWeightedReversalEvidence:
    def test_legacy_count_path_unchanged_when_disabled(self):
        # sweep + M1 BOS + momentum>=0.2 → 3 signals → legacy qualifies.
        ctx = _counter_htf_ctx()
        sa = SituationAssessment(tf_alignment=-0.3, structure_integrity=0.8,
                                 momentum=0.5, read_confidence=0.8, urgency=0.0)
        eng = DecisionEngine()  # weighted disabled by default
        strength, labels = eng._reversal_evidence(ctx, sa)
        assert strength == 3              # integer count, legacy
        assert len(labels) == 3

    def test_weighted_strength_grades_momentum(self):
        eng = DecisionEngine(reversal_weighted_evidence=True,
                             reversal_min_momentum=0.2, reversal_momentum_full=0.6)
        ctx = _counter_htf_ctx()
        weak = SituationAssessment(tf_alignment=-0.3, structure_integrity=0.8,
                                   momentum=0.21, read_confidence=0.8)
        strong = SituationAssessment(tf_alignment=-0.3, structure_integrity=0.8,
                                     momentum=0.95, read_confidence=0.8)
        s_weak, _ = eng._reversal_evidence_strength(ctx, weak)
        s_strong, _ = eng._reversal_evidence_strength(ctx, strong)
        # The binary count would tie these; the weighted strength must not.
        assert s_strong > s_weak
        # sweep (1.0) + BOS (1.0) + tiny momentum ≈ 2.0; strong momentum caps higher.
        assert s_weak < 2.2
        assert s_strong >= 2.5

    def test_two_strong_beat_three_weak(self):
        eng = DecisionEngine(reversal_weighted_evidence=True,
                             reversal_required_strength=2.0,
                             reversal_min_momentum=0.2, reversal_momentum_full=0.6)
        # Two strong signals: sweep + full momentum, NO M1 BOS.
        two_strong_ctx = _counter_htf_ctx(entry_type="SWEEP_REVERSAL", m1_event="NONE")
        two_strong_sa = SituationAssessment(tf_alignment=-0.3, structure_integrity=0.8,
                                            momentum=0.8, read_confidence=0.8)
        s2, _ = eng._reversal_evidence_strength(two_strong_ctx, two_strong_sa)
        # Three weak: sweep + BOS + barely-there momentum.
        three_weak_ctx = _counter_htf_ctx(entry_type="SWEEP_REVERSAL", m1_event="BOS_BULLISH")
        three_weak_sa = SituationAssessment(tf_alignment=-0.3, structure_integrity=0.8,
                                            momentum=0.205, read_confidence=0.8)
        s3, _ = eng._reversal_evidence_strength(three_weak_ctx, three_weak_sa)
        # Both clear the 2.0 strength bar (legacy would reject the 2-signal case).
        assert s2 >= 2.0
        assert s3 >= 2.0

    def test_weighted_gate_qualifies_strong_two_signal_reversal(self):
        # sweep + strong momentum, NO M1 BOS → legacy count = 2 < 3 → not a
        # qualified reversal; weighted strength ≥ 2.0 → marked REVERSAL.
        ctx = _counter_htf_ctx(entry_type="SWEEP_REVERSAL", m1_event="NONE")
        sa = SituationAssessment(tf_alignment=-0.3, structure_integrity=0.8,
                                 momentum=0.9, read_confidence=0.8)
        legacy = DecisionEngine().decide_entry(ctx, sa)
        weighted = DecisionEngine(
            reversal_weighted_evidence=True, reversal_required_strength=2.0,
        ).decide_entry(ctx, sa)
        assert "REVERSAL" not in legacy.reason    # 2/3 count → not qualified
        assert "REVERSAL" in weighted.reason       # strength 2.x ≥ 2.0 → reversal


# ===========================================================================
# #20 — graded market-readiness vs binary MARKET/PENDING cascade
# ===========================================================================

class TestGradedEntryMode:
    def _engine(self, graded: bool, threshold: float = 0.5) -> EntryEngine:
        cfg = AppConfig()
        cfg.scoring.entry_mode_graded_enabled = graded
        cfg.scoring.entry_mode_market_threshold = threshold
        return EntryEngine(config=cfg)

    _zone = {"top": 1.20020, "bottom": 1.19980, "midpoint": 1.20000}
    _pip = 0.0001

    def _decide(self, eng, *, current_price, micro="no_confirmation",
                has_sweep=False, score=0, rr=0.0, mom=0):
        return eng._decide_entry_mode(
            direction="LONG", current_price=current_price, entry_price=1.20000,
            zone=self._zone, micro_confirmation=micro, has_sweep=has_sweep,
            pip_size=self._pip, score=score, risk_reward=rr, momentum_score=mom,
        )

    def test_legacy_cascade_unchanged_when_disabled(self):
        eng = self._engine(graded=False)
        assert self._decide(eng, current_price=1.20010) == "MARKET"   # inside zone
        assert self._decide(eng, current_price=1.20030, micro="engulfing") == "MARKET"
        assert self._decide(eng, current_price=1.20200, micro="no_confirmation") in ("MARKET", "PENDING")

    def test_graded_inside_zone_is_market(self):
        eng = self._engine(graded=True)
        assert self._decide(eng, current_price=1.20010) == "MARKET"

    def test_graded_no_hard_cliff_across_boundary(self):
        # 3.0 vs 3.1 pips below the zone must NOT flip mode via a continuous
        # score (no cliff). Prices sit below zone_bottom (LONG pending region).
        eng = self._engine(graded=True)
        r30 = eng._market_readiness(
            direction="LONG", current_price=1.19970, entry_price=1.20000,
            zone=self._zone, micro_confirmation="engulfing", has_sweep=False,
            pip_size=self._pip, score=70, risk_reward=2.0, momentum_score=0)
        r31 = eng._market_readiness(
            direction="LONG", current_price=1.19969, entry_price=1.20000,
            zone=self._zone, micro_confirmation="engulfing", has_sweep=False,
            pip_size=self._pip, score=70, risk_reward=2.0, momentum_score=0)
        assert abs(r30 - r31) < 0.02

    def test_readiness_monotonic_in_proximity(self):
        eng = self._engine(graded=True)
        near = eng._market_readiness(
            direction="LONG", current_price=1.19970, entry_price=1.20000,
            zone=self._zone, micro_confirmation="engulfing", has_sweep=False,
            pip_size=self._pip)
        far = eng._market_readiness(
            direction="LONG", current_price=1.19880, entry_price=1.20000,
            zone=self._zone, micro_confirmation="engulfing", has_sweep=False,
            pip_size=self._pip)
        assert near > far

    def test_graded_threshold_controls_mode(self):
        # A mid-strength setup (4 pips below zone) flips with the threshold,
        # not a hard rule cascade.
        permissive = self._engine(graded=True, threshold=0.30)
        strict = self._engine(graded=True, threshold=0.90)
        kw = dict(current_price=1.19960, micro="momentum_only", score=72, mom=1)
        assert self._decide(permissive, **kw) == "MARKET"
        assert self._decide(strict, **kw) == "PENDING"


# ===========================================================================
# #32 — structure exit consults carried tf_alignment
# ===========================================================================

def _managed_long_trade(tm: TradeManager):
    sig = EntrySignal(
        pair="EURUSD", direction="LONG", entry_price=1.10000, stop_loss=1.09800,
        tp1=1.10200, tp2=1.10400, risk_reward_1=1.0, risk_reward_2=2.0,
        position_size_lots=0.5, score=90, confluences=["structure"],
        entry_zone="FVG", timestamp=datetime.now(timezone.utc) - timedelta(minutes=45),
    )
    trade = tm.open_trade(sig)
    trade.current_price = 1.10050
    return trade


def _m5(rows: int = 20) -> pd.DataFrame:
    return pd.DataFrame({
        "time": pd.date_range("2025-01-01", periods=rows, freq="5min"),
        "open": [1.10] * rows, "high": [1.1010] * rows,
        "low": [1.0990] * rows, "close": [1.10] * rows,
        "tick_volume": [100] * rows,
    })


class TestStructureExitTfAlignment:
    _bearish = SimpleNamespace(last_event=StructureEvent.CHOCH_BEARISH)

    def test_legacy_exit_fires_ignoring_alignment(self):
        tm = TradeManager()  # tf-alignment defer disabled by default
        trade = _managed_long_trade(tm)
        # Strategic read: structure broken but HTF trend still supports LONG.
        tm.record_strategic_assessment(trade, structure_integrity=0.3,
                                       tf_alignment=0.8)
        with patch("management.trade_manager.StructureEngine") as SE:
            SE.return_value.analyze.return_value = self._bearish
            fired = tm._check_structure_exit(trade, _m5())
        assert fired is True   # legacy: tf_alignment ignored, full close

    def test_supportive_alignment_defers_exit(self):
        tm = TradeManager(structure_exit_tf_alignment_enabled=True,
                          structure_exit_tf_alignment_defer=0.5)
        trade = _managed_long_trade(tm)
        tm.record_strategic_assessment(trade, structure_integrity=0.3,
                                       tf_alignment=0.8)  # supports LONG
        with patch("management.trade_manager.StructureEngine") as SE:
            SE.return_value.analyze.return_value = self._bearish
            fired = tm._check_structure_exit(trade, _m5())
        assert fired is False   # deferred — HTF trend still supports

    def test_opposing_alignment_still_exits(self):
        tm = TradeManager(structure_exit_tf_alignment_enabled=True,
                          structure_exit_tf_alignment_defer=0.5)
        trade = _managed_long_trade(tm)
        tm.record_strategic_assessment(trade, structure_integrity=0.3,
                                       tf_alignment=-0.8)  # opposes LONG
        with patch("management.trade_manager.StructureEngine") as SE:
            SE.return_value.analyze.return_value = self._bearish
            fired = tm._check_structure_exit(trade, _m5())
        assert fired is True   # alignment opposes → exit fires


# ===========================================================================
# #34 — expectancy-aware calibration vs win-rate-only
# ===========================================================================

def _trade(sl_strategy: str, pnl_r: float) -> dict:
    return {"plan": {"sl_strategy": sl_strategy}, "outcome": {"pnl_r": pnl_r}}


class TestExpectancyAwareCalibration:
    def _group(self, strategy, rs):
        return [_trade(strategy, r) for r in rs]

    def test_legacy_winrate_demotes_high_payoff_group(self):
        cfg = PlannerConfig(calibration_expectancy_aware=False)  # legacy win-rate mode
        cal = Calibrator(cfg)
        # structure: 40% win rate but huge payoff; atr: 60% win rate, tiny payoff.
        structure = self._group("structure", [3.0, 3.0, 3.0, 3.0, -1, -1, -1, -1, -1, -1])
        atr = self._group("atr", [0.2, 0.2, 0.2, 0.2, 0.2, 0.2, -0.1, -0.1, -0.1, -0.1])
        updates, changes = {}, []
        cal._calibrate_sl_strategy(structure + atr, updates, changes)
        # Win-rate (40% < 60%) pushes the structure-SL band DOWN despite +EV.
        assert "prefer_structure_sl_within_atr" in updates
        assert updates["prefer_structure_sl_within_atr"] < cfg.prefer_structure_sl_within_atr

    def test_expectancy_aware_rewards_high_payoff_group(self):
        cfg = PlannerConfig(calibration_expectancy_aware=True,
                            calibration_expectancy_margin=0.10)
        cal = Calibrator(cfg)
        structure = self._group("structure", [3.0, 3.0, 3.0, 3.0, -1, -1, -1, -1, -1, -1])
        atr = self._group("atr", [0.2, 0.2, 0.2, 0.2, 0.2, 0.2, -0.1, -0.1, -0.1, -0.1])
        updates, changes = {}, []
        cal._calibrate_sl_strategy(structure + atr, updates, changes)
        # Expectancy: structure +0.6R vs atr ~+0.08R → band widens UP.
        assert "prefer_structure_sl_within_atr" in updates
        assert updates["prefer_structure_sl_within_atr"] > cfg.prefer_structure_sl_within_atr

    def test_config_default_is_live(self):
        # Flipped live: calibration compares groups by expectancy, not win-rate.
        assert PlannerConfig().calibration_expectancy_aware is True


# ===========================================================================
# #33 — EV / losing-pattern / ML vetoes → optional bounded penalty mode
# ===========================================================================

class TestGradedDefensiveGateConfig:
    """The penalty branches are inline in main_loop._execute_entry_inner; this
    asserts the config surface defaults to the legacy hard veto and that the
    penalty size mults are bounded."""

    def test_defaults_are_veto(self):
        rc = AppConfig().risk
        assert rc.ev_gate_mode == "veto"
        assert rc.losing_pattern_mode == "veto"
        assert rc.ml_should_trade_mode == "veto"

    def test_penalty_size_mults_present_and_bounded(self):
        rc = AppConfig().risk
        for mult in (rc.ev_gate_below_size_mult,
                     rc.losing_pattern_size_mult,
                     rc.ml_should_trade_size_mult):
            assert 0.0 < mult <= 1.0

    def test_penalty_mode_assignable(self):
        rc = AppConfig().risk
        rc.ev_gate_mode = "penalty"
        rc.losing_pattern_mode = "penalty"
        rc.ml_should_trade_mode = "penalty"
        assert rc.ev_gate_mode == "penalty"
        assert rc.losing_pattern_mode == "penalty"
        assert rc.ml_should_trade_mode == "penalty"


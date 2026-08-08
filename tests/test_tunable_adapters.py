"""
Tests for the Tunable adapters (adaptive/tunable_adapters.py).

Verifies each adapter faithfully wraps its underlying component: metadata
(frequency / dependencies / cadence), get_current_params reflecting real state,
should_tune respecting the data floor + interval, validate_params catching
out-of-bounds values, tune delegating to the real method, and rollback
restoring the prior snapshot. Persistence paths are redirected to tmp_path so
tests never touch the repo's data/ directory.
"""

import time

import pytest

from adaptive.tunable import TuneContext, TuneFrequency
from adaptive.tunable_adapters import (
    ScoreOptimizerTunable,
    RegimeLearnerTunable,
    PairLearnerTunable,
    SessionLearnerTunable,
    EVEstimatorTunable,
    GateTunerTunable,
    PlannerCalibratorTunable,
    SignalLedgerTunable,
)


def _ctx(total=100, **kw):
    return TuneContext(total_trades=total, **kw)


def _trade(pnl=1.0, tags=None, pair="EURUSD", regime="trending", session="london", **kw):
    d = {
        "pnl": pnl,
        "pnl_dollars": pnl,
        "risk_dollars": 1.0,
        "pair": pair,
        "regime": regime,
        "session": session,
        "confluences_tags": tags or ["structure", "fvg"],
    }
    d.update(kw)
    return d


# ── ScoreOptimizer ──────────────────────────────────────────────────────


class TestScoreOptimizerTunable:
    @pytest.fixture
    def opt(self, tmp_path, monkeypatch):
        from adaptive.score_optimizer import ScoreOptimizer

        monkeypatch.setattr(ScoreOptimizer, "DEFAULT_PATH", str(tmp_path / "w.json"))
        return ScoreOptimizer()

    def test_metadata(self, opt):
        t = ScoreOptimizerTunable(opt, lambda: [])
        assert t.tunable_name == "score_optimizer"
        assert t.frequency == TuneFrequency.ON_TRADE_BATCH
        assert t.dependencies == []
        assert t.min_trades_required == 50

    def test_current_params_reflect_weights(self, opt):
        t = ScoreOptimizerTunable(opt, lambda: [])
        params = t.get_current_params()
        assert params["structure"] == opt.current_weights.structure_weight

    def test_should_tune_respects_data_floor(self, opt):
        t = ScoreOptimizerTunable(opt, lambda: [], min_trades=50)
        assert t.should_tune(_ctx(total=10)) is False
        assert t.should_tune(_ctx(total=60)) is True

    def test_validate_rejects_out_of_envelope(self, opt):
        t = ScoreOptimizerTunable(opt, lambda: [])
        bad = opt.current_weights.as_dict()
        bad["structure"] = 999
        ok, why = t.validate_params(bad)
        assert ok is False and "structure" in why

    def test_tune_no_trades_is_skipped(self, opt):
        t = ScoreOptimizerTunable(opt, lambda: [])
        res = t.tune(_ctx())
        assert res.skipped is True and res.success is True

    def test_tune_delegates_and_on_update_fires(self, opt):
        seen = {}
        trades = [_trade(pnl=1.0, tags=["fvg"]) for _ in range(40)] + \
                 [_trade(pnl=-1.0, tags=["wyckoff"]) for _ in range(40)]
        t = ScoreOptimizerTunable(
            opt, lambda: trades, on_update=lambda w: seen.update({"w": w}),
        )
        res = t.tune(_ctx())
        assert res.success is True
        # optimize ran on the provided trades (on_update fires only if changed).
        assert res.params_after  # populated

    def test_rollback_restores_weights(self, opt):
        t = ScoreOptimizerTunable(opt, lambda: [])
        before = t.get_current_params()
        t._begin()  # snapshot
        opt.current_weights.structure_weight = 7
        assert t.rollback() is True
        assert t.get_current_params()["structure"] == before["structure"]


# ── Regime / Pair / Session learners ────────────────────────────────────


class TestRegimeLearnerTunable:
    @pytest.fixture
    def learner(self, tmp_path, monkeypatch):
        from adaptive.regime_learner import RegimeLearner

        monkeypatch.setattr(RegimeLearner, "SAVE_PATH", str(tmp_path / "r.json"))
        return RegimeLearner()

    def test_metadata(self, learner):
        t = RegimeLearnerTunable(learner, lambda: [])
        assert t.tunable_name == "regime_learner"
        assert t.min_trades_required == 30

    def test_tune_populates_params(self, learner):
        trades = [_trade(pnl=1.0, regime="trending") for _ in range(40)]
        t = RegimeLearnerTunable(learner, lambda: trades)
        res = t.tune(_ctx())
        assert res.success is True
        assert "trending" in t.get_current_params()

    def test_validate_rejects_bad_tp(self, learner):
        t = RegimeLearnerTunable(learner, lambda: [])
        ok, why = t.validate_params({"trending": {"optimal_tp_multiplier": 9.0}})
        assert ok is False and "tp_multiplier" in why

    def test_rollback_restores_state(self, learner):
        trades = [_trade(pnl=1.0, regime="trending") for _ in range(40)]
        t = RegimeLearnerTunable(learner, lambda: trades)
        t._begin()
        t.tune(_ctx())
        # snapshot was empty; rollback should clear learned strategies.
        assert t.rollback() is True
        assert t.get_current_params() == {}


class TestPairLearnerTunable:
    @pytest.fixture
    def learner(self, tmp_path, monkeypatch):
        from adaptive.pair_learner import PairLearner

        monkeypatch.setattr(PairLearner, "SAVE_PATH", str(tmp_path / "p.json"))
        return PairLearner()

    def test_metadata(self, learner):
        t = PairLearnerTunable(learner, lambda: [])
        assert t.tunable_name == "pair_learner"
        assert t.min_trades_required == 10

    def test_tune_populates_profile(self, learner):
        trades = [_trade(pnl=1.0, pair="EURUSD") for _ in range(30)]
        t = PairLearnerTunable(learner, lambda: trades)
        t.tune(_ctx())
        assert "EURUSD" in t.get_current_params()

    def test_validate_rejects_bad_winrate(self, learner):
        t = PairLearnerTunable(learner, lambda: [])
        ok, why = t.validate_params({"EURUSD": {"win_rate": 2.0, "recommendation": "TRADE"}})
        assert ok is False and "win_rate" in why

    def test_validate_rejects_unknown_recommendation(self, learner):
        t = PairLearnerTunable(learner, lambda: [])
        ok, why = t.validate_params({"EURUSD": {"win_rate": 0.5, "recommendation": "WAT"}})
        assert ok is False and "recommendation" in why


class TestSessionLearnerTunable:
    @pytest.fixture
    def learner(self, tmp_path, monkeypatch):
        from adaptive.session_learner import SessionLearner

        monkeypatch.setattr(SessionLearner, "SAVE_PATH", str(tmp_path / "s.json"))
        return SessionLearner()

    def test_metadata(self, learner):
        t = SessionLearnerTunable(learner, lambda: [])
        assert t.tunable_name == "session_learner"
        assert t.min_trades_required == 20

    def test_validate_rejects_unknown_recommendation(self, learner):
        t = SessionLearnerTunable(learner, lambda: [])
        ok, why = t.validate_params({"london": {"win_rate": 0.5, "recommendation": "X"}})
        assert ok is False


# ── EV estimator ────────────────────────────────────────────────────────


class TestEVEstimatorTunable:
    def test_metadata_depends_on_pair_learner(self):
        from adaptive.ev_estimator import EVEstimator

        t = EVEstimatorTunable(EVEstimator(), refresh=lambda: 5)
        assert t.tunable_name == "ev_estimator"
        assert t.frequency == TuneFrequency.ON_TRADE_CLOSE
        assert t.dependencies == ["pair_learner"]

    def test_tune_refreshes_history_size(self):
        from adaptive.ev_estimator import EVEstimator

        t = EVEstimatorTunable(EVEstimator(), refresh=lambda: 42)
        res = t.tune(_ctx())
        assert res.success is True
        assert t.get_current_params()["history_size"] == 42

    def test_no_refresh_hook_skips(self):
        from adaptive.ev_estimator import EVEstimator

        t = EVEstimatorTunable(EVEstimator(), refresh=None)
        res = t.tune(_ctx())
        assert res.skipped is True

    def test_rollback_is_noop_true(self):
        from adaptive.ev_estimator import EVEstimator

        t = EVEstimatorTunable(EVEstimator(), refresh=lambda: 1)
        assert t.rollback() is True


# ── Gate tuner ──────────────────────────────────────────────────────────


class TestGateTunerTunable:
    @pytest.fixture
    def tuner(self, tmp_path):
        from adaptive.gate_tuner import GateTuner

        return GateTuner(path=str(tmp_path / "g.json"))

    def test_metadata_depends_on_ev(self, tuner):
        t = GateTunerTunable(tuner, lambda: [])
        assert t.tunable_name == "gate_tuner"
        assert t.frequency == TuneFrequency.PERIODIC
        assert t.dependencies == ["ev_estimator"]

    def test_validate_rejects_non_tunable_family(self, tuner):
        t = GateTunerTunable(tuner, lambda: [])
        ok, why = t.validate_params({"governor": 0.5})
        assert ok is False and "non-tunable" in why

    def test_validate_rejects_out_of_bounds_offset(self, tuner):
        t = GateTunerTunable(tuner, lambda: [])
        ok, why = t.validate_params({"ev_gate": 5.0})  # bound is [-0.10, 0.0]
        assert ok is False

    def test_tune_loosens_on_winning_rejections(self, tuner):
        # 40 resolved shadows for ev_gate, mostly WINs → loosen.
        outcomes = [
            {"rejecting_gate": "ev_gate", "outcome": "WIN", "cnt": 30, "avg_r": 1.0},
            {"rejecting_gate": "ev_gate", "outcome": "LOSS", "cnt": 5, "avg_r": -1.0},
        ]
        t = GateTunerTunable(tuner, lambda: outcomes)
        res = t.tune(_ctx())
        assert res.success is True
        assert res.changed is True
        assert tuner.offset("ev_gate") < 0.0  # loosened (negative offset)


# ── Planner calibrator ──────────────────────────────────────────────────


class TestPlannerCalibratorTunable:
    @pytest.fixture
    def calibrator(self):
        from dataclasses import replace
        from planning.calibrator import Calibrator
        from planning.trade_planner import PlannerConfig

        cfg = PlannerConfig()
        cfg = replace(cfg, calibration_enabled=True, calibration_min_trades=8,
                      calibration_interval_trades=1)
        return Calibrator(cfg)

    def _completed(self, sl_strategy, wins, total):
        out = []
        for i in range(total):
            r = 1.0 if i < wins else -1.0
            out.append({
                "plan": {"sl_strategy": sl_strategy, "confidence": 0.5},
                "outcome": {"pnl_r": r, "outcome": "tp" if r > 0 else "stop"},
            })
        return out

    def test_metadata_depends_on_regime(self, calibrator):
        t = PlannerCalibratorTunable(calibrator, lambda: [])
        assert t.tunable_name == "planner_calibrator"
        assert t.dependencies == ["regime_learner"]

    def test_current_params_are_the_four_fields(self, calibrator):
        t = PlannerCalibratorTunable(calibrator, lambda: [])
        params = t.get_current_params()
        assert set(params) == {
            "prefer_structure_sl_within_atr", "limit_order_zone_distance_atr",
            "default_be_trigger_r", "min_confidence_to_enter",
        }

    def test_validate_rejects_bad_confidence(self, calibrator):
        t = PlannerCalibratorTunable(calibrator, lambda: [])
        ok, why = t.validate_params({"min_confidence_to_enter": 5.0})
        assert ok is False

    def test_tune_moves_structure_preference(self, calibrator):
        completed = (
            self._completed("structure", wins=9, total=10)
            + self._completed("atr", wins=2, total=10)
        )
        before = calibrator.config.prefer_structure_sl_within_atr
        t = PlannerCalibratorTunable(calibrator, lambda: completed, planner=None)
        res = t.tune(_ctx())
        assert res.success is True
        after = calibrator.config.prefer_structure_sl_within_atr
        assert after > before  # structure stops won more → preference widened

    def test_tune_skips_when_not_due(self, calibrator):
        t = PlannerCalibratorTunable(calibrator, lambda: [])  # 0 completed < min
        res = t.tune(_ctx())
        assert res.skipped is True


# ── Signal ledger grading ───────────────────────────────────────────────


class TestSignalLedgerTunable:
    @pytest.fixture
    def ledger(self, tmp_path):
        from adaptive.signal_ledger import SignalLedger

        led = SignalLedger(
            db_path=tmp_path / "sig.db",
            grading_delay_minutes=0.0,
            check_intervals=[5, 15, 30],
            min_move_pct=0.1,
        )
        yield led
        led.close()

    def test_metadata(self, ledger):
        t = SignalLedgerTunable(ledger, prices_provider=None)
        assert t.tunable_name == "signal_ledger"
        assert t.frequency == TuneFrequency.PER_SCAN_CYCLE
        assert t.should_tune(_ctx()) is True  # always per cycle

    def test_no_prices_is_skipped(self, ledger):
        t = SignalLedgerTunable(ledger, prices_provider=None)
        res = t.tune(_ctx())
        assert res.skipped is True

    def test_grading_runs_with_prices(self, ledger):
        from adaptive.signal_ledger import SignalRecord

        ledger.record_signal(SignalRecord(
            pair="EURUSD", emitter="momentum", direction="LONG",
            strength=0.8, price_at_signal=100.0,
        ))
        t = SignalLedgerTunable(ledger, prices_provider=None)
        res = t.tune(_ctx(current_prices={"EURUSD": 101.0}))
        assert res.success is True

    def test_rollback_is_noop_true(self, ledger):
        t = SignalLedgerTunable(ledger, prices_provider=None)
        assert t.rollback() is True


# ── Cross-adapter: shared scheduling semantics ──────────────────────────


class TestSchedulingSemantics:
    def test_interval_floor_blocks_until_elapsed(self):
        from adaptive.ev_estimator import EVEstimator

        t = EVEstimatorTunable(EVEstimator(), refresh=lambda: 1, min_interval=3600)
        # First run allowed, then mark tuned and ensure interval blocks.
        assert t.should_tune(_ctx()) is True
        t._last_tune_ts = time.time()
        assert t.should_tune(_ctx()) is False

    def test_batch_cadence_requires_new_trades(self, tmp_path, monkeypatch):
        from adaptive.pair_learner import PairLearner

        monkeypatch.setattr(PairLearner, "SAVE_PATH", str(tmp_path / "p.json"))
        t = PairLearnerTunable(PairLearner(), lambda: [], min_trades=10)
        t._last_tune_ts = time.time() - 100_000  # interval (4h) satisfied
        t._trades_at_last_tune = 100
        assert t.should_tune(_ctx(total=105)) is False   # only 5 new
        assert t.should_tune(_ctx(total=115)) is True     # 15 new ≥ 10

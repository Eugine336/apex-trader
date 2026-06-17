"""
Tests for the Vote Calibrator (adaptive/vote_calibrator.py) and its TunerAgent
adapter (adaptive/tunable_adapters.VoteCalibratorTunable).

Covers the core math (mean-1.0 neutral property, accurate-louder ordering, the
three weight methods, Bayesian shrinkage, floor/ceiling clamping, sample-size
gating), the enabled/disabled passthrough on the hot path, recalibration skip
paths, config validation, and the agent integration (registration metadata,
agent-driven tune, validation rejection + rollback, and the no-bypass guard).

Dependency-light: a tiny fake EmitterFeedback drives the math precisely, plus
one faithful integration test over a real SignalLedger + EmitterFeedbackService.
"""

import math
import os
import tempfile

import pytest

from config import VoteCalibratorConfig
from adaptive.vote_calibrator import (
    VoteCalibrator,
    VoteCalibration,
    DEFAULT_VOTE_MODULES,
)
from adaptive.tunable_adapters import VoteCalibratorTunable
from adaptive.tunable import TuneContext, TuneFrequency
from adaptive.tuner_agent import TunerAgent, EXPECTED_TUNABLES


# ── Test doubles ──────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, accuracy_all, total_signals):
        self.accuracy_all = accuracy_all
        self.total_signals = total_signals


class _FakeFeedback:
    """Minimal stand-in for EmitterFeedbackService.get_all_emitter_summaries."""

    def __init__(self, data):
        # data: {module: (accuracy, n)}
        self._data = data
        self.last_lookback = None

    def get_all_emitter_summaries(self, lookback=100):
        self.last_lookback = lookback
        return {m: _FakeResp(a, n) for m, (a, n) in self._data.items()}


class _BoomFeedback:
    def get_all_emitter_summaries(self, lookback=100):
        raise RuntimeError("boom")


def _cfg(**kw):
    base = dict(vote_calibration_enabled=True)
    base.update(kw)
    return VoteCalibratorConfig(**base)


# ── Core math ─────────────────────────────────────────────────────────────


class TestCalibrationMath:
    def test_neutral_panel_is_no_op(self):
        """A panel of equally-accurate modules produces all-1.0 multipliers."""
        fb = _FakeFeedback({m: (0.6, 100) for m in
                            ("momentum", "structure", "vwap", "liquidity")})
        vc = VoteCalibrator(_cfg(), fb)
        cal = vc.recalibrate()
        assert not cal.skipped
        for w in cal.multipliers.values():
            assert w == pytest.approx(1.0, abs=1e-6)

    def test_mean_multiplier_is_one(self):
        """Multipliers are centred so their mean is ~1.0 (only re-balances)."""
        fb = _FakeFeedback({
            "momentum": (0.8, 100), "structure": (0.4, 100),
            "vwap": (0.6, 100), "liquidity": (0.5, 100),
        })
        vc = VoteCalibrator(_cfg(vote_weight_temperature=0.5), fb)
        cal = vc.recalibrate()
        mean = sum(cal.multipliers.values()) / len(cal.multipliers)
        assert mean == pytest.approx(1.0, abs=0.05)

    def test_accurate_module_votes_louder(self):
        fb = _FakeFeedback({
            "momentum": (0.85, 100), "structure": (0.35, 100), "vwap": (0.6, 100),
        })
        vc = VoteCalibrator(_cfg(), fb)
        vc.recalibrate()
        assert vc.multiplier_for("momentum") > vc.multiplier_for("vwap")
        assert vc.multiplier_for("vwap") > vc.multiplier_for("structure")

    @pytest.mark.parametrize("method", ["softmax", "proportional", "log_odds"])
    def test_all_methods_order_by_accuracy(self, method):
        fb = _FakeFeedback({
            "momentum": (0.8, 100), "structure": (0.4, 100), "vwap": (0.6, 100),
        })
        vc = VoteCalibrator(_cfg(vote_weight_method=method), fb)
        cal = vc.recalibrate()
        assert cal.method == method
        assert cal.multipliers["momentum"] > cal.multipliers["structure"]

    def test_floor_and_ceiling_clamp(self):
        """Extreme accuracy spread cannot silence or let a module dominate."""
        fb = _FakeFeedback({
            "momentum": (0.99, 200), "structure": (0.01, 200), "vwap": (0.5, 200),
        })
        vc = VoteCalibrator(
            _cfg(vote_weight_method="softmax", vote_weight_temperature=0.05,
                 vote_weight_floor=0.25, vote_weight_ceiling=2.5),
            fb,
        )
        cal = vc.recalibrate()
        for w in cal.multipliers.values():
            assert 0.25 - 1e-9 <= w <= 2.5 + 1e-9
        assert cal.multipliers["momentum"] == pytest.approx(2.5, abs=1e-6)
        assert cal.multipliers["structure"] == pytest.approx(0.25, abs=1e-6)

    def test_min_signals_gate_holds_thin_module_at_one(self):
        fb = _FakeFeedback({
            "momentum": (0.9, 100), "structure": (0.3, 100),
            "vwap": (0.95, 3),  # below min_signals
        })
        vc = VoteCalibrator(_cfg(vote_calibration_min_signals=20), fb)
        cal = vc.recalibrate()
        assert cal.multipliers["vwap"] == pytest.approx(1.0, abs=1e-9)
        assert cal.qualifying_modules == 2

    def test_fewer_than_two_qualifying_is_all_neutral(self):
        fb = _FakeFeedback({"momentum": (0.9, 100), "structure": (0.3, 5)})
        vc = VoteCalibrator(_cfg(vote_calibration_min_signals=20), fb)
        cal = vc.recalibrate()
        assert cal.skipped
        assert all(w == pytest.approx(1.0) for w in cal.multipliers.values())

    def test_shrinkage_pulls_thin_sample_toward_mean(self):
        """A high-accuracy but thin-sample module is pulled toward the mean more
        than the same accuracy on a large sample."""
        common = {"structure": (0.4, 500), "vwap": (0.5, 500)}
        fb_thin = _FakeFeedback({**common, "momentum": (0.9, 25)})
        fb_thick = _FakeFeedback({**common, "momentum": (0.9, 500)})
        vc_thin = VoteCalibrator(
            _cfg(vote_calibration_min_signals=20, vote_calibration_shrinkage=1.0), fb_thin)
        vc_thick = VoteCalibrator(
            _cfg(vote_calibration_min_signals=20, vote_calibration_shrinkage=1.0), fb_thick)
        vc_thin.recalibrate()
        vc_thick.recalibrate()
        # Thin sample → shrunk harder → smaller boost than the thick sample.
        assert vc_thin.multiplier_for("momentum") < vc_thick.multiplier_for("momentum")

    def test_zero_shrinkage_uses_raw_accuracy(self):
        fb = _FakeFeedback({
            "momentum": (0.8, 30), "structure": (0.4, 30), "vwap": (0.6, 30),
        })
        vc = VoteCalibrator(_cfg(vote_calibration_shrinkage=0.0), fb)
        cal = vc.recalibrate()
        # With no shrinkage the accuracy used equals the raw accuracy.
        assert cal.accuracies["momentum"] == pytest.approx(0.8, abs=1e-9)


# ── Hot-path weight application ─────────────────────────────────────────────


class TestWeightApplication:
    def test_disabled_returns_base_weight_unchanged(self):
        fb = _FakeFeedback({"momentum": (0.9, 100), "structure": (0.3, 100)})
        vc = VoteCalibrator(_cfg(vote_calibration_enabled=False), fb)
        vc.recalibrate()
        assert vc.calibrated_weight("momentum", 3.0) == 3.0
        assert vc.multiplier_for("momentum") == 1.0

    def test_enabled_scales_base_weight(self):
        fb = _FakeFeedback({
            "momentum": (0.85, 100), "structure": (0.35, 100), "vwap": (0.6, 100),
        })
        vc = VoteCalibrator(_cfg(), fb)
        vc.recalibrate()
        mult = vc.multiplier_for("momentum")
        assert vc.calibrated_weight("momentum", 2.0) == pytest.approx(2.0 * mult)

    def test_unknown_module_is_neutral(self):
        fb = _FakeFeedback({"momentum": (0.85, 100), "structure": (0.35, 100)})
        vc = VoteCalibrator(_cfg(), fb)
        vc.recalibrate()
        assert vc.multiplier_for("does_not_exist") == 1.0

    def test_no_calibration_run_yet_is_neutral(self):
        vc = VoteCalibrator(_cfg(), _FakeFeedback({}))
        assert vc.multiplier_for("momentum") == 1.0
        assert vc.calibrated_weight("momentum", 5.0) == 5.0

    def test_non_numeric_base_weight_passthrough(self):
        vc = VoteCalibrator(_cfg(vote_calibration_enabled=False), _FakeFeedback({}))
        assert vc.calibrated_weight("momentum", None) is None


# ── Recalibration skip / failure paths ──────────────────────────────────────


class TestRecalibrateSkips:
    def test_disabled_skips(self):
        vc = VoteCalibrator(_cfg(vote_calibration_enabled=False),
                            _FakeFeedback({"momentum": (0.9, 100)}))
        cal = vc.recalibrate()
        assert cal.skipped and "disabled" in cal.reason

    def test_no_feedback_wired_skips(self):
        vc = VoteCalibrator(_cfg(), emitter_feedback=None)
        cal = vc.recalibrate()
        assert cal.skipped and "feedback" in cal.reason.lower()

    def test_feedback_exception_is_swallowed(self):
        vc = VoteCalibrator(_cfg(), _BoomFeedback())
        cal = vc.recalibrate()
        assert cal.skipped
        # Previously-published map (empty) stays intact; no raise.
        assert vc.get_weight_multipliers() == {}

    def test_lookback_passed_through(self):
        fb = _FakeFeedback({"momentum": (0.8, 100), "structure": (0.4, 100)})
        vc = VoteCalibrator(_cfg(vote_calibration_lookback=250), fb)
        vc.recalibrate()
        assert fb.last_lookback == 250

    def test_consensus_emitter_is_ignored(self):
        fb = _FakeFeedback({
            "momentum": (0.8, 100), "structure": (0.4, 100),
            "consensus": (0.99, 100),  # not an independent module
        })
        vc = VoteCalibrator(_cfg(), fb)
        cal = vc.recalibrate()
        assert "consensus" not in cal.multipliers


# ── restore_multipliers (rollback support) ──────────────────────────────────


class TestRestore:
    def test_restore_filters_non_finite(self):
        vc = VoteCalibrator(_cfg(), _FakeFeedback({}))
        vc.restore_multipliers({"momentum": 1.5, "structure": float("nan"),
                                "vwap": float("inf"), "bad": "x"})
        mults = vc.get_weight_multipliers()
        assert mults == {"momentum": 1.5}

    def test_restore_none_clears(self):
        vc = VoteCalibrator(_cfg(), _FakeFeedback({}))
        vc.restore_multipliers({"momentum": 1.5})
        vc.restore_multipliers(None)
        assert vc.get_weight_multipliers() == {}


# ── Config validation ───────────────────────────────────────────────────────


class TestConfigValidation:
    def test_default_is_off(self):
        assert VoteCalibratorConfig().vote_calibration_enabled is False

    def test_bad_method_rejected(self):
        with pytest.raises(ValueError):
            VoteCalibratorConfig(vote_weight_method="nope")

    def test_non_positive_temperature_rejected(self):
        with pytest.raises(ValueError):
            VoteCalibratorConfig(vote_weight_temperature=0.0)

    def test_negative_floor_rejected(self):
        with pytest.raises(ValueError):
            VoteCalibratorConfig(vote_weight_floor=-0.1)

    def test_ceiling_below_floor_rejected(self):
        with pytest.raises(ValueError):
            VoteCalibratorConfig(vote_weight_floor=1.0, vote_weight_ceiling=0.5)

    def test_min_signals_must_be_positive(self):
        with pytest.raises(ValueError):
            VoteCalibratorConfig(vote_calibration_min_signals=0)

    def test_negative_shrinkage_rejected(self):
        with pytest.raises(ValueError):
            VoteCalibratorConfig(vote_calibration_shrinkage=-1.0)

    def test_lookback_must_be_positive(self):
        with pytest.raises(ValueError):
            VoteCalibratorConfig(vote_calibration_lookback=0)


# ── TunerAgent adapter ──────────────────────────────────────────────────────


@pytest.fixture
def agent_db(tmp_path):
    return str(tmp_path / "tuner_audit.db")


class TestVoteCalibratorTunable:
    def test_metadata(self):
        vc = VoteCalibrator(_cfg(), _FakeFeedback({}))
        t = VoteCalibratorTunable(vc, min_interval=900.0)
        assert t.tunable_name == "vote_calibrator"
        assert t.frequency == TuneFrequency.PERIODIC
        assert t.dependencies == ["signal_ledger"]
        assert t.min_interval_seconds == 900.0

    def test_in_expected_tunables(self):
        assert "vote_calibrator" in EXPECTED_TUNABLES

    def test_validate_rejects_out_of_band(self):
        vc = VoteCalibrator(_cfg(), _FakeFeedback({}))
        t = VoteCalibratorTunable(vc)
        ok, _ = t.validate_params(
            {"multipliers": {"momentum": 9.0}, "floor": 0.1, "ceiling": 3.0})
        assert not ok

    def test_validate_rejects_non_finite(self):
        vc = VoteCalibrator(_cfg(), _FakeFeedback({}))
        t = VoteCalibratorTunable(vc)
        ok, _ = t.validate_params(
            {"multipliers": {"momentum": float("inf")}, "floor": 0.0, "ceiling": 5.0})
        assert not ok

    def test_validate_accepts_in_band(self):
        vc = VoteCalibrator(_cfg(), _FakeFeedback({}))
        t = VoteCalibratorTunable(vc)
        ok, _ = t.validate_params(
            {"multipliers": {"momentum": 1.5}, "floor": 0.1, "ceiling": 3.0})
        assert ok

    def test_tune_skips_when_disabled(self, agent_db):
        vc = VoteCalibrator(_cfg(vote_calibration_enabled=False),
                            _FakeFeedback({"momentum": (0.9, 100)}))
        t = VoteCalibratorTunable(vc)
        res = t.tune(TuneContext(force=True))
        assert res.success and res.skipped

    def test_agent_drives_tune_and_publishes(self, agent_db):
        fb = _FakeFeedback({
            "momentum": (0.85, 100), "structure": (0.35, 100), "vwap": (0.6, 100),
        })
        vc = VoteCalibrator(_cfg(vote_weight_temperature=0.5), fb)
        agent = TunerAgent(enabled=True, audit_db_path=agent_db)
        agent.register(VoteCalibratorTunable(vc, min_interval=0.0))
        res = agent.force_tune_all(TuneContext(total_trades=200, force=True))
        vc_res = [r for r in res if r.tunable_name == "vote_calibrator"][0]
        assert vc_res.success and vc_res.changed and not vc_res.skipped
        assert vc.multiplier_for("momentum") > vc.multiplier_for("structure")
        agent.close()

    def test_no_bypass_when_agent_sole_authority(self, agent_db):
        fb = _FakeFeedback({"momentum": (0.85, 100), "structure": (0.35, 100)})
        vc = VoteCalibrator(_cfg(), fb)
        agent = TunerAgent(enabled=True, audit_db_path=agent_db)
        agent.register(VoteCalibratorTunable(vc, min_interval=0.0))
        vc.set_tuner_agent(agent)
        # Direct call is blocked → skipped, multipliers untouched (still empty).
        cal = vc.recalibrate()
        assert cal.skipped and "blocked" in cal.reason.lower()
        assert vc.get_weight_multipliers() == {}
        agent.close()

    def test_agent_rolls_back_invalid_output(self, agent_db, monkeypatch):
        """If a recompute somehow yields out-of-band multipliers, the agent's
        validate→rollback restores the prior published map."""
        fb = _FakeFeedback({
            "momentum": (0.8, 100), "structure": (0.4, 100), "vwap": (0.6, 100),
        })
        vc = VoteCalibrator(_cfg(), fb)
        # Seed a known-good published map first.
        vc._recalibrate_unguarded()
        good = vc.get_weight_multipliers()

        agent = TunerAgent(enabled=True, audit_db_path=agent_db)
        t = VoteCalibratorTunable(vc, min_interval=0.0)
        agent.register(t)

        # Force the next published map to be out-of-band on read-back.
        def _poison():
            vc._multipliers = {"momentum": 999.0}
            return VoteCalibration(multipliers={"momentum": 999.0}, skipped=False)
        monkeypatch.setattr(vc, "recalibrate", _poison)

        agent.force_tune_all(TuneContext(total_trades=200, force=True))
        # Validation should have failed → rollback to the prior good map.
        assert vc.get_weight_multipliers() == good
        agent.close()


# ── Integration over a real ledger + feedback service ───────────────────────


class TestIntegrationWithRealLedger:
    def test_end_to_end_from_graded_signals(self, tmp_path):
        from adaptive.signal_ledger import SignalLedger, SignalRecord
        from adaptive.emitter_feedback import EmitterFeedbackService

        led = SignalLedger(db_path=tmp_path / "sl.db", grading_delay_minutes=0.0,
                           min_move_pct=0.1)
        # momentum: mostly correct LONGs; structure: mostly wrong LONGs.
        # price moves up after, so LONG = correct.
        for i in range(30):
            led.record_signal(SignalRecord(
                pair="EURUSD", emitter="momentum",
                direction="LONG", price_at_signal=100.0))
        for i in range(30):
            led.record_signal(SignalRecord(
                pair="EURUSD", emitter="structure",
                direction="SHORT", price_at_signal=100.0))
        # Grade: price rose to 101 → LONGs correct, SHORTs wrong.
        led.run_grading_cycle({"EURUSD": 101.0})

        svc = EmitterFeedbackService(led)
        vc = VoteCalibrator(
            _cfg(vote_calibration_min_signals=10, vote_weight_temperature=0.5), svc)
        cal = vc.recalibrate()
        led.close()

        assert not cal.skipped
        assert cal.multipliers["momentum"] > cal.multipliers["structure"]
        assert vc.calibrated_weight("momentum", 1.0) > 1.0
        assert vc.calibrated_weight("structure", 1.0) < 1.0

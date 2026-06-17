"""
Tests for the L3↔L4 loop closure: the Module Governor and the Vote Calibrator
consuming the Counterfactual engine's marginal-R signal.

Covers, for the governor:
  * SHADOW on the counterfactual signal alone (accuracy fine, marginal R harmful).
  * The min-trades trust floor (thin attribution data is ignored).
  * use_counterfactual_signal=False ⇒ accuracy-only behaviour (no regression).
  * "both" trigger when accuracy AND attribution both say harmful.
  * Reactivation AND-gate (accuracy recovered but marginal R still negative ⇒
    stays SHADOW; both acceptable ⇒ reactivates).
  * DISABLE on the counterfactual signal alone.
  * Enriched transition trigger + marginal_r persisted/exposed.

And for the calibrator:
  * Marginal R damps an accurate-but-harmful module below an accurate-and-helpful one.
  * use_counterfactual_weight=False ⇒ accuracy-only (unchanged).
  * Thin attribution data (below min trades) ⇒ no blend.

A controllable fake CounterfactualEngine + EmitterFeedback drive the policy
precisely; no DB / live loop required.
"""

import os
import tempfile

import pytest

from config import ModuleGovernorConfig, VoteCalibratorConfig
from adaptive.module_governor import ModuleGovernor, ModuleMode
from adaptive.vote_calibrator import VoteCalibrator


# ── Test doubles ──────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, accuracy_all, total_signals):
        self.accuracy_all = accuracy_all
        self.total_signals = total_signals


class _FakeFeedback:
    """EmitterFeedback stand-in keyed by module → (accuracy, n)."""

    def __init__(self, data=None):
        self._data = dict(data or {})

    def set(self, module, accuracy, n):
        self._data[module] = (accuracy, n)

    def request_feedback(self, request):
        acc, n = self._data.get(getattr(request, "emitter", ""), (0.0, 0))
        return _FakeResp(acc, n)

    def get_all_emitter_summaries(self, lookback=100):
        return {m: _FakeResp(a, n) for m, (a, n) in self._data.items()}


class _FakeCounterfactual:
    """Stand-in exposing only get_cached_attributions(), like the real engine.

    ``data``: {module: (marginal_r_total, trades_involved, better_off_without)}.
    """

    def __init__(self, data=None):
        self._data = dict(data or {})

    def set(self, module, marginal_r, trades, better_off_without):
        self._data[module] = (marginal_r, trades, better_off_without)

    def get_cached_attributions(self):
        modules = [
            {
                "module": m,
                "marginal_r": mr,
                "trades_involved": tr,
                "better_off_without": bow,
            }
            for m, (mr, tr, bow) in self._data.items()
        ]
        return {"modules": modules}


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _gov(tmp_dir, feedback, counterfactual=None, **cfg_kw):
    base = dict(module_governor_enabled=True)
    base.update(cfg_kw)
    return ModuleGovernor(
        ModuleGovernorConfig(**base),
        emitter_feedback=feedback,
        counterfactual=counterfactual,
        db_path=os.path.join(tmp_dir, "mg.db"),
        modules=("momentum", "structure"),
    )


# ── Governor: counterfactual signal ────────────────────────────────────────


class TestGovernorCounterfactualShadow:
    def test_shadow_on_counterfactual_alone(self, tmp_dir):
        # Accuracy is healthy but marginal R is net-negative with enough trades.
        fb = _FakeFeedback({"momentum": (0.80, 50), "structure": (0.80, 50)})
        cf = _FakeCounterfactual({"momentum": (-40.0, 200, True)})  # -0.2R/trade
        gov = _gov(
            tmp_dir, fb, cf,
            shadow_threshold=0.35, shadow_lookback=50,
            marginal_r_shadow_threshold=-0.05, marginal_r_min_trades=100,
        )
        moved = {t.module: t for t in gov.evaluate_transitions()}
        assert gov.mode_for("momentum") == ModuleMode.SHADOW
        assert gov.mode_for("structure") == ModuleMode.ACTIVE
        assert moved["momentum"].trigger == "counterfactual"
        assert moved["momentum"].marginal_r == pytest.approx(-0.2, abs=1e-6)
        assert "marginal_R" in moved["momentum"].reason

    def test_thin_attribution_is_ignored(self, tmp_dir):
        # Same harmful marginal R but below the min-trades trust floor.
        fb = _FakeFeedback({"momentum": (0.80, 50)})
        cf = _FakeCounterfactual({"momentum": (-20.0, 40, True)})
        gov = _gov(
            tmp_dir, fb, cf,
            marginal_r_shadow_threshold=-0.05, marginal_r_min_trades=100,
        )
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.ACTIVE

    def test_better_off_without_required(self, tmp_dir):
        # Negative marginal R but engine did NOT flag better_off_without.
        fb = _FakeFeedback({"momentum": (0.80, 50)})
        cf = _FakeCounterfactual({"momentum": (-40.0, 200, False)})
        gov = _gov(tmp_dir, fb, cf, marginal_r_min_trades=100)
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.ACTIVE

    def test_signal_off_is_accuracy_only(self, tmp_dir):
        fb = _FakeFeedback({"momentum": (0.80, 50)})
        cf = _FakeCounterfactual({"momentum": (-40.0, 200, True)})
        gov = _gov(
            tmp_dir, fb, cf,
            use_counterfactual_signal=False, marginal_r_min_trades=100,
        )
        gov.evaluate_transitions()
        # Counterfactual ignored entirely; healthy accuracy keeps it ACTIVE.
        assert gov.mode_for("momentum") == ModuleMode.ACTIVE

    def test_both_signals_trigger_label(self, tmp_dir):
        # Accuracy poor AND marginal R harmful ⇒ trigger "both".
        fb = _FakeFeedback({"momentum": (0.20, 50)})
        cf = _FakeCounterfactual({"momentum": (-40.0, 200, True)})
        gov = _gov(
            tmp_dir, fb, cf,
            shadow_threshold=0.35, shadow_lookback=50, marginal_r_min_trades=100,
        )
        moved = {t.module: t for t in gov.evaluate_transitions()}
        assert gov.mode_for("momentum") == ModuleMode.SHADOW
        assert moved["momentum"].trigger == "both"


class TestGovernorReactivationAndGate:
    def _shadowed(self, tmp_dir, cf):
        # First demote momentum to SHADOW on poor accuracy.
        fb = _FakeFeedback({"momentum": (0.20, 50)})
        gov = _gov(
            tmp_dir, fb, cf,
            shadow_threshold=0.35, shadow_lookback=50,
            reactivation_threshold=0.50, reactivation_min_signals=30,
            marginal_r_min_trades=100, marginal_r_reactivation_threshold=0.0,
        )
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.SHADOW
        return gov, fb

    def test_reactivation_blocked_when_marginal_r_below_floor(self, tmp_dir):
        # Marginal R sits in the middle zone: below the reactivation floor (so
        # it blocks the AND-gate) but not harmful enough to disable.
        cf = _FakeCounterfactual({"momentum": (-6.0, 200, False)})  # -0.03R/trade
        gov, fb = self._shadowed(tmp_dir, cf)
        # Accuracy recovers over the shadow window, but marginal R is still < 0.
        fb.set("momentum", 0.70, 90)  # shadow_n = 90 - 50 baseline = 40 >= 30
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.SHADOW

    def test_disable_when_accuracy_recovers_but_marginal_r_harmful(self, tmp_dir):
        # Directional accuracy looks recovered, but attribution proves the module
        # is costing money (better_off_without) ⇒ DISABLED, not reactivated.
        cf = _FakeCounterfactual({"momentum": (-40.0, 200, True)})  # -0.2R/trade
        gov, fb = self._shadowed(tmp_dir, cf)
        fb.set("momentum", 0.70, 90)
        moved = {t.module: t for t in gov.evaluate_transitions()}
        assert gov.mode_for("momentum") == ModuleMode.DISABLED
        assert moved["momentum"].trigger == "counterfactual"

    def test_reactivation_when_both_acceptable(self, tmp_dir):
        cf = _FakeCounterfactual({"momentum": (40.0, 200, False)})  # +0.2R/trade
        gov, fb = self._shadowed(tmp_dir, cf)
        fb.set("momentum", 0.70, 90)
        moved = {t.module: t for t in gov.evaluate_transitions()}
        assert gov.mode_for("momentum") == ModuleMode.ACTIVE
        assert moved["momentum"].trigger == "both"

    def test_disable_on_counterfactual_alone(self, tmp_dir):
        # In SHADOW with accuracy that neither recovers nor is disable-low, the
        # marginal-R signal alone confirms harmful ⇒ DISABLED.
        cf = _FakeCounterfactual({"momentum": (-40.0, 200, True)})
        gov, fb = self._shadowed(tmp_dir, cf)
        fb.set("momentum", 0.45, 90)  # < reactivation 0.50, > disable 0.25
        moved = {t.module: t for t in gov.evaluate_transitions()}
        assert gov.mode_for("momentum") == ModuleMode.DISABLED
        assert moved["momentum"].trigger == "counterfactual"


class TestGovernorStatusExposesMarginalR:
    def test_status_carries_marginal_r(self, tmp_dir):
        fb = _FakeFeedback({"momentum": (0.80, 50), "structure": (0.80, 50)})
        cf = _FakeCounterfactual({"momentum": (-40.0, 200, True)})
        gov = _gov(tmp_dir, fb, cf, marginal_r_min_trades=100)
        status = gov.get_status()
        assert status["counterfactual_signal"] is True
        by_mod = {m["module"]: m for m in status["modules"]}
        assert by_mod["momentum"]["marginal_r"] == pytest.approx(-0.2, abs=1e-6)
        assert by_mod["momentum"]["better_off_without"] is True
        # structure has no attribution row → marginal_r is None.
        assert by_mod["structure"]["marginal_r"] is None


# ── Vote calibrator: counterfactual blend ──────────────────────────────────


def _vc_cfg(**kw):
    base = dict(vote_calibration_enabled=True)
    base.update(kw)
    return VoteCalibratorConfig(**base)


class TestVoteCalibratorCounterfactualBlend:
    def test_marginal_r_damps_harmful_module(self):
        # Equal accuracy ⇒ accuracy multipliers ~1.0 each. Marginal R then
        # separates them: the net-negative module is damped below the helpful one.
        fb = _FakeFeedback({"momentum": (0.6, 100), "structure": (0.6, 100)})
        cf = _FakeCounterfactual({
            "momentum": (-40.0, 200, True),   # -0.2R/trade
            "structure": (40.0, 200, False),  # +0.2R/trade
        })
        vc = VoteCalibrator(_vc_cfg(counterfactual_weight_min_trades=100), fb)
        vc.set_counterfactual(cf)
        cal = vc.recalibrate()
        assert cal.counterfactual_used is True
        assert cal.multipliers["momentum"] < cal.multipliers["structure"]
        # Panel stays mean-centred on ~1.0.
        mean = sum(cal.multipliers.values()) / len(cal.multipliers)
        assert mean == pytest.approx(1.0, abs=0.05)

    def test_blend_off_is_accuracy_only(self):
        fb = _FakeFeedback({"momentum": (0.6, 100), "structure": (0.6, 100)})
        cf = _FakeCounterfactual({
            "momentum": (-40.0, 200, True),
            "structure": (40.0, 200, False),
        })
        vc = VoteCalibrator(_vc_cfg(use_counterfactual_weight=False), fb)
        vc.set_counterfactual(cf)
        cal = vc.recalibrate()
        assert cal.counterfactual_used is False
        # Equal accuracy ⇒ both neutral.
        assert cal.multipliers["momentum"] == pytest.approx(
            cal.multipliers["structure"], abs=1e-6
        )

    def test_thin_attribution_skips_blend(self):
        fb = _FakeFeedback({"momentum": (0.6, 100), "structure": (0.6, 100)})
        cf = _FakeCounterfactual({
            "momentum": (-10.0, 40, True),    # below min trades
            "structure": (10.0, 40, False),   # below min trades
        })
        vc = VoteCalibrator(_vc_cfg(counterfactual_weight_min_trades=100), fb)
        vc.set_counterfactual(cf)
        cal = vc.recalibrate()
        assert cal.counterfactual_used is False
        assert cal.multipliers["momentum"] == pytest.approx(
            cal.multipliers["structure"], abs=1e-6
        )

    def test_no_engine_wired_is_accuracy_only(self):
        fb = _FakeFeedback({"momentum": (0.8, 100), "structure": (0.4, 100)})
        vc = VoteCalibrator(_vc_cfg(), fb)  # no counterfactual set
        cal = vc.recalibrate()
        assert cal.counterfactual_used is False
        # Accuracy ordering preserved (accurate louder).
        assert cal.multipliers["momentum"] > cal.multipliers["structure"]


# ── Config validation ──────────────────────────────────────────────────────


class TestConfigValidation:
    def test_governor_counterfactual_defaults(self):
        cfg = ModuleGovernorConfig()
        assert cfg.use_counterfactual_signal is True
        assert cfg.marginal_r_min_trades == 100

    def test_governor_reactivation_below_shadow_rejected(self):
        with pytest.raises(ValueError):
            ModuleGovernorConfig(
                marginal_r_shadow_threshold=0.0,
                marginal_r_reactivation_threshold=-0.1,
            )

    def test_calibrator_blend_bounds(self):
        with pytest.raises(ValueError):
            VoteCalibratorConfig(counterfactual_weight_blend=1.5)
        with pytest.raises(ValueError):
            VoteCalibratorConfig(counterfactual_weight_min_trades=0)

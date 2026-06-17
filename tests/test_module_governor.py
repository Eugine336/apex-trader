"""
Tests for the Module Governor (adaptive/module_governor.py) and its TunerAgent
adapter (adaptive/tunable_adapters.ModuleGovernorTunable).

Covers the three-state machine (ACTIVE → SHADOW → ACTIVE/DISABLED), the sample
floors that protect low-data modules, shadow-period accounting for reactivation
/ disable decisions, the disabled flag / enabled passthrough, manual override,
auto-retry, persistence across reopen, the transition audit log, the scanner's
vote-suppression enforcement, config validation, and the agent integration
(registration metadata, agent-driven tune, validation rejection, no-bypass guard).

Dependency-light: a controllable fake EmitterFeedback drives the policy
precisely; one test exercises the real PairScanner._vote_weight enforcement path.
"""

import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from config import ModuleGovernorConfig
from adaptive.module_governor import (
    DEFAULT_GOVERNED_MODULES,
    GovernorTransition,
    ModuleGovernor,
    ModuleMode,
)
from adaptive.tunable import TuneContext, TuneFrequency
from adaptive.tunable_adapters import ModuleGovernorTunable
from adaptive.tuner_agent import TunerAgent


# ── Test doubles ──────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, accuracy_all, total_signals):
        self.accuracy_all = accuracy_all
        self.total_signals = total_signals


class _FakeFeedback:
    """Controllable EmitterFeedback stand-in keyed by module → (accuracy, n)."""

    def __init__(self, data=None):
        # data: {module: (accuracy, n)}
        self._data = dict(data or {})
        self.last_lookback = None

    def set(self, module, accuracy, n):
        self._data[module] = (accuracy, n)

    def request_feedback(self, request):
        self.last_lookback = getattr(request, "lookback", None)
        acc, n = self._data.get(getattr(request, "emitter", ""), (0.0, 0))
        return _FakeResp(acc, n)


class _BoomFeedback:
    def request_feedback(self, request):
        raise RuntimeError("boom")


def _cfg(**kw):
    base = dict(module_governor_enabled=True)
    base.update(kw)
    return ModuleGovernorConfig(**base)


def _make_governor(tmp_path, feedback, **cfg_kw):
    db = os.path.join(tmp_path, "mg.db")
    return ModuleGovernor(
        _cfg(**cfg_kw),
        emitter_feedback=feedback,
        db_path=db,
        modules=("momentum", "structure"),
    )


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


# ── Disabled / passthrough ─────────────────────────────────────────────────


class TestDisabled:
    def test_disabled_feature_never_suppresses(self, tmp_dir):
        fb = _FakeFeedback({"momentum": (0.0, 999)})
        gov = ModuleGovernor(
            ModuleGovernorConfig(module_governor_enabled=False),
            emitter_feedback=fb,
            db_path=os.path.join(tmp_dir, "mg.db"),
            modules=("momentum",),
        )
        # Even a terrible module is never shadowed when the feature is off.
        assert gov.evaluate_transitions() == []
        assert gov.is_suppressed("momentum") is False
        assert gov.mode_for("momentum") == ModuleMode.ACTIVE

    def test_unknown_module_is_active(self, tmp_dir):
        gov = _make_governor(tmp_dir, _FakeFeedback())
        assert gov.mode_for("does_not_exist") == ModuleMode.ACTIVE
        assert gov.is_suppressed("does_not_exist") is False


# ── ACTIVE → SHADOW ─────────────────────────────────────────────────────────


class TestActiveToShadow:
    def test_demotes_when_poor_over_full_window(self, tmp_dir):
        fb = _FakeFeedback({"momentum": (0.20, 50), "structure": (0.80, 50)})
        gov = _make_governor(tmp_dir, fb, shadow_threshold=0.35, shadow_lookback=50)
        transitions = gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.SHADOW
        assert gov.mode_for("structure") == ModuleMode.ACTIVE
        moved = {t.module: t for t in transitions}
        assert "momentum" in moved
        assert moved["momentum"].old_mode == "ACTIVE"
        assert moved["momentum"].new_mode == "SHADOW"

    def test_low_sample_module_not_punished(self, tmp_dir):
        # Accuracy is terrible but the window isn't full → stays ACTIVE.
        fb = _FakeFeedback({"momentum": (0.05, 10)})
        gov = _make_governor(tmp_dir, fb, shadow_threshold=0.35, shadow_lookback=50)
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.ACTIVE

    def test_accurate_module_stays_active(self, tmp_dir):
        fb = _FakeFeedback({"momentum": (0.70, 100)})
        gov = _make_governor(tmp_dir, fb, shadow_threshold=0.35, shadow_lookback=50)
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.ACTIVE


# ── SHADOW → ACTIVE / DISABLED ──────────────────────────────────────────────


class TestShadowTransitions:
    def _shadow_momentum(self, tmp_dir, fb, **kw):
        gov = _make_governor(tmp_dir, fb, shadow_threshold=0.35, shadow_lookback=50, **kw)
        fb.set("momentum", 0.20, 50)
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.SHADOW
        return gov

    def test_reactivates_on_recovery(self, tmp_dir):
        fb = _FakeFeedback()
        gov = self._shadow_momentum(
            tmp_dir, fb, reactivation_threshold=0.50, reactivation_min_signals=30,
        )
        # 30 more shadow signals (baseline was 50) at recovered accuracy.
        fb.set("momentum", 0.60, 80)
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.ACTIVE
        assert gov.is_suppressed("momentum") is False

    def test_stays_shadow_when_too_few_shadow_signals(self, tmp_dir):
        fb = _FakeFeedback()
        gov = self._shadow_momentum(
            tmp_dir, fb, reactivation_threshold=0.50, reactivation_min_signals=30,
        )
        # Only 10 new shadow signals — below reactivation_min_signals.
        fb.set("momentum", 0.90, 60)
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.SHADOW

    def test_disables_when_stays_poor(self, tmp_dir):
        fb = _FakeFeedback()
        gov = self._shadow_momentum(
            tmp_dir, fb, disable_threshold=0.25, disable_min_signals=50,
        )
        # 50 more shadow signals still terrible → DISABLED.
        fb.set("momentum", 0.10, 100)
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.DISABLED
        assert gov.is_disabled("momentum") is True
        assert gov.is_suppressed("momentum") is True

    def test_suppressed_for_shadow_and_disabled(self, tmp_dir):
        fb = _FakeFeedback()
        gov = self._shadow_momentum(tmp_dir, fb)
        assert gov.is_shadowed("momentum") is True
        assert gov.is_suppressed("momentum") is True
        # structure stayed active
        assert gov.is_suppressed("structure") is False


# ── Manual override + auto-retry ────────────────────────────────────────────


class TestOverrideAndRetry:
    def test_force_mode_records_transition(self, tmp_dir):
        gov = _make_governor(tmp_dir, _FakeFeedback())
        assert gov.force_mode("momentum", ModuleMode.SHADOW, reason="manual test") is True
        assert gov.mode_for("momentum") == ModuleMode.SHADOW
        trans = gov.get_transitions()
        assert any(t["new_mode"] == "SHADOW" and t["module"] == "momentum" for t in trans)

    def test_force_mode_unknown_module(self, tmp_dir):
        gov = _make_governor(tmp_dir, _FakeFeedback())
        assert gov.force_mode("nope", ModuleMode.SHADOW) is False

    def test_auto_retry_disabled_to_shadow(self, tmp_dir):
        fb = _FakeFeedback()
        gov = _make_governor(
            tmp_dir, fb, shadow_threshold=0.35, shadow_lookback=50,
            disable_threshold=0.25, disable_min_signals=50, auto_retry_days=1,
        )
        # Drive momentum ACTIVE→SHADOW→DISABLED.
        fb.set("momentum", 0.20, 50)
        gov.evaluate_transitions()
        fb.set("momentum", 0.10, 100)
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.DISABLED
        # Backdate the disabled_time by 2 days, then re-evaluate → SHADOW.
        with gov._lock:
            gov._state["momentum"].disabled_time = time.time() - 2 * 86400
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.SHADOW

    def test_auto_retry_off_keeps_disabled(self, tmp_dir):
        fb = _FakeFeedback()
        gov = _make_governor(
            tmp_dir, fb, shadow_threshold=0.35, shadow_lookback=50,
            disable_threshold=0.25, disable_min_signals=50, auto_retry_days=0,
        )
        fb.set("momentum", 0.20, 50)
        gov.evaluate_transitions()
        fb.set("momentum", 0.10, 100)
        gov.evaluate_transitions()
        with gov._lock:
            gov._state["momentum"].disabled_time = time.time() - 365 * 86400
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.DISABLED


# ── Persistence + audit + status ────────────────────────────────────────────


class TestPersistenceAndStatus:
    def test_state_survives_reopen(self, tmp_dir):
        db = os.path.join(tmp_dir, "mg.db")
        fb = _FakeFeedback({"momentum": (0.20, 50), "structure": (0.80, 50)})
        gov = ModuleGovernor(_cfg(shadow_lookback=50), emitter_feedback=fb,
                             db_path=db, modules=("momentum", "structure"))
        gov.evaluate_transitions()
        assert gov.mode_for("momentum") == ModuleMode.SHADOW
        gov.close()
        # Reopen — the SHADOW mode must be restored from disk.
        gov2 = ModuleGovernor(_cfg(shadow_lookback=50), emitter_feedback=fb,
                              db_path=db, modules=("momentum", "structure"))
        assert gov2.mode_for("momentum") == ModuleMode.SHADOW
        assert gov2.is_suppressed("momentum") is True

    def test_status_shape(self, tmp_dir):
        gov = _make_governor(tmp_dir, _FakeFeedback())
        status = gov.get_status()
        assert status["enabled"] is True
        assert status["module_count"] == 2
        assert {m["module"] for m in status["modules"]} == {"momentum", "structure"}
        assert status["counts"]["ACTIVE"] == 2

    def test_transitions_recorded(self, tmp_dir):
        fb = _FakeFeedback({"momentum": (0.20, 50)})
        gov = _make_governor(tmp_dir, fb, shadow_lookback=50)
        gov.evaluate_transitions()
        trans = gov.get_transitions(limit=10)
        assert len(trans) >= 1
        assert trans[0]["module"] == "momentum"
        assert trans[0]["new_mode"] == "SHADOW"

    def test_feedback_failure_is_safe(self, tmp_dir):
        gov = ModuleGovernor(_cfg(), emitter_feedback=_BoomFeedback(),
                             db_path=os.path.join(tmp_dir, "mg.db"),
                             modules=("momentum",))
        # Must not raise; no transitions, module stays ACTIVE.
        assert gov.evaluate_transitions() == []
        assert gov.mode_for("momentum") == ModuleMode.ACTIVE


# ── Scanner enforcement ─────────────────────────────────────────────────────


class TestScannerEnforcement:
    def test_vote_weight_zeroed_for_shadow(self, tmp_dir):
        from scanner.pair_scanner import PairScanner

        fb = _FakeFeedback()
        gov = _make_governor(tmp_dir, fb, shadow_lookback=50)
        fb.set("momentum", 0.20, 50)
        gov.evaluate_transitions()
        assert gov.is_suppressed("momentum") is True

        cc = SimpleNamespace(weights={"momentum": 1.0, "structure": 3.0})
        fake_self = SimpleNamespace(_module_governor=gov, _vote_calibrator=None)
        # momentum is shadowed → weight forced to 0.0
        assert PairScanner._vote_weight(fake_self, cc, "momentum", 1.0) == 0.0
        # structure is active → keeps its base weight
        assert PairScanner._vote_weight(fake_self, cc, "structure", 3.0) == 3.0

    def test_vote_weight_passthrough_when_no_governor(self, tmp_dir):
        from scanner.pair_scanner import PairScanner

        cc = SimpleNamespace(weights={"momentum": 1.0})
        fake_self = SimpleNamespace(_module_governor=None, _vote_calibrator=None)
        assert PairScanner._vote_weight(fake_self, cc, "momentum", 1.0) == 1.0


# ── Config validation ───────────────────────────────────────────────────────


class TestConfig:
    def test_defaults_off(self):
        assert ModuleGovernorConfig().module_governor_enabled is False

    def test_threshold_bounds(self):
        with pytest.raises(ValueError):
            ModuleGovernorConfig(shadow_threshold=1.5)

    def test_disable_must_be_below_shadow(self):
        with pytest.raises(ValueError):
            ModuleGovernorConfig(disable_threshold=0.6, shadow_threshold=0.4)

    def test_min_signals_positive(self):
        with pytest.raises(ValueError):
            ModuleGovernorConfig(reactivation_min_signals=0)

    def test_auto_retry_non_negative(self):
        with pytest.raises(ValueError):
            ModuleGovernorConfig(auto_retry_days=-1)


# ── TunerAgent integration ──────────────────────────────────────────────────


class TestTunerAgentIntegration:
    def test_adapter_metadata(self, tmp_dir):
        gov = _make_governor(tmp_dir, _FakeFeedback())
        t = ModuleGovernorTunable(gov)
        assert t.tunable_name == "module_governor"
        assert t.frequency == TuneFrequency.PERIODIC
        assert "signal_ledger" in t.dependencies

    def test_agent_driven_tune(self, tmp_dir):
        fb = _FakeFeedback({"momentum": (0.20, 50), "structure": (0.80, 50)})
        gov = _make_governor(tmp_dir, fb, shadow_lookback=50)
        agent = TunerAgent(enabled=True, audit_db_path=os.path.join(tmp_dir, "audit.db"))
        gov.set_tuner_agent(agent)
        agent.register(ModuleGovernorTunable(gov, min_interval=0.0))
        results = agent.force_tune_all(TuneContext(force=True))
        names = {r.tunable_name for r in results}
        assert "module_governor" in names
        assert gov.mode_for("momentum") == ModuleMode.SHADOW

    def test_direct_call_blocked_when_agent_authority(self, tmp_dir):
        fb = _FakeFeedback({"momentum": (0.20, 50)})
        gov = _make_governor(tmp_dir, fb, shadow_lookback=50)
        agent = TunerAgent(enabled=True, audit_db_path=os.path.join(tmp_dir, "audit.db"))
        gov.set_tuner_agent(agent)
        # Direct (un-authorised) evaluate is blocked → no transition.
        assert gov.evaluate_transitions() == []
        assert gov.mode_for("momentum") == ModuleMode.ACTIVE

    def test_validate_rejects_bad_mode(self, tmp_dir):
        gov = _make_governor(tmp_dir, _FakeFeedback())
        t = ModuleGovernorTunable(gov)
        ok, _ = t.validate_params({"modes": {"momentum": "BOGUS"}})
        assert ok is False
        ok2, _ = t.validate_params({"modes": {"momentum": "SHADOW"}})
        assert ok2 is True

    def test_rollback_restores_modes(self, tmp_dir):
        fb = _FakeFeedback({"momentum": (0.20, 50)})
        gov = _make_governor(tmp_dir, fb, shadow_lookback=50)
        t = ModuleGovernorTunable(gov)
        before = t.get_current_params()
        assert before["modes"]["momentum"] == "ACTIVE"
        t._begin()  # snapshot ACTIVE state
        gov._evaluate_unguarded()
        assert gov.mode_for("momentum") == ModuleMode.SHADOW
        assert t.rollback() is True
        assert gov.mode_for("momentum") == ModuleMode.ACTIVE


def test_default_governed_modules_cover_nine():
    assert len(DEFAULT_GOVERNED_MODULES) == 9
    assert "consensus" not in DEFAULT_GOVERNED_MODULES

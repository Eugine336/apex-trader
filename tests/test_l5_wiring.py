"""
Integration tests for the L5 wiring in platforms/main_loop.py and the dashboard
LearningMixin, exercised without a full broker stack.

Verifies that the gated engine construction (``_init_evolution_engines``), the
parameter current-values provider, the promote-callback application to live
config, and the dashboard aggregation all behave correctly — using the real
methods bound to a light stand-in object.
"""

import os
import tempfile
import types

import pytest

from config import AppConfig
from adaptive.counterfactual import CounterfactualEngine
from dashboard.state_learning import LearningMixin
from platforms.main_loop import TradingLoop as MainLoop


@pytest.fixture
def cf_engine():
    d = tempfile.mkdtemp()
    eng = CounterfactualEngine(db_path=os.path.join(d, "cf.db"), enabled=True)
    yield eng
    eng.close()


def _config_with_l5(tmp):
    cfg = AppConfig()
    cfg.param_evolution.param_evolution_enabled = True
    cfg.param_evolution.param_evolution_db_path = os.path.join(tmp, "pe.db")
    cfg.signal_discovery.signal_discovery_enabled = True
    cfg.signal_discovery.signal_discovery_db_path = os.path.join(tmp, "sd.db")
    return cfg


def _fake_loop(cfg, cf_engine):
    """A stand-in carrying just what the bound MainLoop L5 methods touch.

    The parameter evolver is constructed with the loop's own current-values
    provider + promote callback, so bind those real methods onto the stub.
    """
    loop = types.SimpleNamespace(config=cfg, _counterfactual=cf_engine)
    loop._param_evolution_current_values = types.MethodType(
        MainLoop._param_evolution_current_values, loop,
    )
    loop._param_evolution_apply = types.MethodType(
        MainLoop._param_evolution_apply, loop,
    )
    return loop


# ── _init_evolution_engines ──────────────────────────────────────────────────

def test_init_builds_engines_when_enabled(cf_engine):
    tmp = tempfile.mkdtemp()
    loop = _fake_loop(_config_with_l5(tmp), cf_engine)
    MainLoop._init_evolution_engines(loop)
    assert loop._param_evolver is not None
    assert loop._signal_discovery is not None
    assert loop._param_evolver.enabled is True


def test_init_skips_when_no_counterfactual():
    tmp = tempfile.mkdtemp()
    loop = types.SimpleNamespace(config=_config_with_l5(tmp), _counterfactual=None)
    MainLoop._init_evolution_engines(loop)
    assert loop._param_evolver is None
    assert loop._signal_discovery is None


def test_init_none_when_flags_off(cf_engine):
    cfg = AppConfig()
    # Explicitly disable the L5 flags (defaults are live/on) to verify the
    # gated construction still skips engine creation when turned off.
    cfg.param_evolution.param_evolution_enabled = False
    cfg.signal_discovery.signal_discovery_enabled = False
    loop = _fake_loop(cfg, cf_engine)
    MainLoop._init_evolution_engines(loop)
    assert loop._param_evolver is None
    assert loop._signal_discovery is None


# ── current values + promote callback ────────────────────────────────────────

def test_current_values_provider_reads_live_config(cf_engine):
    cfg = AppConfig()
    loop = types.SimpleNamespace(config=cfg)
    vals = MainLoop._param_evolution_current_values(loop)
    assert vals["min_net_score"] == cfg.consensus.min_net_score
    assert vals["min_agreement"] == cfg.consensus.min_agreement
    assert vals["min_contributors"] == cfg.consensus.min_contributors
    assert vals["min_expected_value"] == cfg.opportunity_ranker.min_expected_value
    assert vals["min_cluster_contributors"] == cfg.opportunity_ranker.min_cluster_contributors


def test_promote_applies_threshold_and_ranker_values():
    from adaptive.param_evolution import LOC_RANKER, LOC_THRESHOLD

    cfg = AppConfig()
    loop = types.SimpleNamespace(config=cfg)
    # Float threshold param → consensus config.
    assert MainLoop._param_evolution_apply(loop, "min_net_score", LOC_THRESHOLD, 1.85) is True
    assert cfg.consensus.min_net_score == pytest.approx(1.85)
    # Int param → rounded.
    assert MainLoop._param_evolution_apply(loop, "min_contributors", LOC_THRESHOLD, 2.6) is True
    assert cfg.consensus.min_contributors == 3
    # Ranker param → opportunity_ranker config.
    assert MainLoop._param_evolution_apply(loop, "min_expected_value", LOC_RANKER, 0.4) is True
    assert cfg.opportunity_ranker.min_expected_value == pytest.approx(0.4)


def test_promote_rejects_unknown_param():
    from adaptive.param_evolution import LOC_THRESHOLD

    loop = types.SimpleNamespace(config=AppConfig())
    assert MainLoop._param_evolution_apply(loop, "not_a_param", LOC_THRESHOLD, 1.0) is False


# ── Dashboard aggregation ────────────────────────────────────────────────────

class _DashStub(LearningMixin):
    def __init__(self, loop):
        self.is_live = True
        self._trading_loop = loop


def test_dashboard_exposes_l5_panels(cf_engine):
    tmp = tempfile.mkdtemp()
    cfg = _config_with_l5(tmp)
    loop = _fake_loop(cfg, cf_engine)
    MainLoop._init_evolution_engines(loop)
    dash = _DashStub(loop)
    learning = dash.get_learning()
    for key in ("param_evolution", "signal_discovery"):
        assert key in learning
        assert learning[key]["enabled"] is True
    # The keeper L5b interaction panel is always aggregated (idle here since
    # the analyzer is built in a separate init path, not _init_evolution_engines).
    assert "interactions" in learning


def test_dashboard_graceful_when_engines_absent():
    loop = types.SimpleNamespace(config=AppConfig(), _param_evolver=None,
                                 _signal_discovery=None)
    dash = _DashStub(loop)
    learning = dash.get_learning()
    # Idle (disabled) shape, never an exception.
    assert learning["param_evolution"]["enabled"] is False
    assert learning["signal_discovery"]["enabled"] is False
    assert learning["interactions"]["enabled"] is False
    # The retired module's panel key must be gone.
    assert "module_interaction" not in learning

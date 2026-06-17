"""
Tests for the L5c virtual (synthetic) voting-module lifecycle:

* ``adaptive/virtual_modules.py`` — the registry, definitions, vote computation,
  persistence, kill switch, restart-shadow and no-cascade guarantees.
* ``adaptive/virtual_promotion.py`` — the shadow → promote → retire policy and
  its TunerAgent guard.
* ``adaptive/tunable_adapters.py`` — the VirtualSignalManagerTunable adapter.

Deterministic vote panels + temp SQLite DBs; no live loop, broker or network.
"""

import os
import tempfile
from types import SimpleNamespace

# Bind real numpy/pandas at import time. Some other test modules in the suite
# replace these in sys.modules with mocks during their run and never restore
# them; capturing the genuine modules here keeps the scanner-integration tests
# below robust to that pre-existing cross-test pollution.
import numpy as _np
import pandas as _pd

import pytest

from brain.directional_consensus import Vote, decide
from adaptive.module_governor import ModuleMode
from adaptive.virtual_modules import (
    ABSENT,
    SYNTH_PREFIX,
    VirtualModuleDefinition,
    VirtualModuleRegistry,
    definition_from_rule,
    make_name,
)
from adaptive.virtual_promotion import VirtualSignalManager
from adaptive.tunable import TuneContext
from adaptive.tunable_adapters import VirtualSignalManagerTunable
from adaptive.tuner_agent import TunerAgent


# ── Helpers ────────────────────────────────────────────────────────────────


def _real_votes():
    """Nine-ish real module votes — structure+liquidity LONG, momentum SHORT."""
    return [
        Vote("structure", "LONG", 0.8, 3.0),
        Vote("liquidity", "LONG", 0.7, 1.0),
        Vote("momentum", "SHORT", 0.6, 1.0),
        Vote("volume", "NEUTRAL", 0.0, 1.0),
    ]


def _registry(tmp, *, enabled=True, max_active=5, restart=0):
    return VirtualModuleRegistry(
        enabled=enabled,
        db_path=os.path.join(tmp, "vm.db"),
        max_active=max_active,
        restart_shadow_trades=restart,
    )


def _def(name_conditions, direction="LONG", conf=0.7):
    return VirtualModuleDefinition(
        name=make_name(name_conditions),
        conditions=tuple(name_conditions),
        vote_direction=direction,
        base_confidence=conf,
        source_label="test",
        win_rate=conf,
        edge=0.2,
    )


def _cfg(**over):
    base = dict(
        signal_discovery_enabled=True,   # kill switch on (registry weights live)
        virtual_promotion_enabled=True,
        shadow_trades_required=10,
        min_shadow_accuracy=0.55,
        min_shadow_marginal_r=0.0,
        promotion_min_signals=5,
        max_promotions_per_cycle=1,
        promotion_initial_weight=1.0,
        retirement_accuracy_threshold=0.45,
        retirement_min_signals=5,
        retirement_marginal_r_min_trades=50,
        retirement_marginal_r_threshold=-0.05,
        feedback_lookback=500,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _FakeFeedback:
    """Returns controllable accuracy/sample-size per emitter."""

    def __init__(self, table):
        self._table = table  # {name: (accuracy, n)}

    def request_feedback(self, request):
        acc, n = self._table.get(request.emitter, (0.0, 0))
        return SimpleNamespace(accuracy_all=acc, total_signals=n)


class _FakeCF:
    def __init__(self, modules):
        self._modules = modules  # list of dicts

    def get_cached_attributions(self):
        return {"modules": self._modules}


class _FakeDiscovery:
    def __init__(self, rules, enabled=True):
        self.enabled = enabled
        self._rules = rules

    def get_cached(self):
        return {"rules": self._rules}


# ── Definition + eligibility ────────────────────────────────────────────────


def test_definition_fires_only_when_all_conditions_match():
    d = _def([("structure", "LONG"), ("liquidity", "LONG")])
    assert d.compute_vote({"structure": "LONG", "liquidity": "LONG"}) == ("LONG", 0.7)
    # one condition off → no fire
    assert d.compute_vote({"structure": "LONG", "liquidity": ABSENT}) is None
    assert d.compute_vote({"structure": "SHORT", "liquidity": "LONG"}) is None


def test_definition_from_rule_direction_and_ineligibility():
    long_rule = {"label": "x", "conditions": [["structure", "LONG"], ["liquidity", "LONG"]], "win_rate": 0.62}
    d = definition_from_rule(long_rule)
    assert d is not None and d.vote_direction == "LONG"
    assert abs(d.base_confidence - 0.62) < 1e-9

    short_rule = {"conditions": [["momentum", "SHORT"], ["vwap", "SHORT"]], "win_rate": 0.7}
    assert definition_from_rule(short_rule).vote_direction == "SHORT"

    # Balanced / non-directional → ineligible (None).
    balanced = {"conditions": [["structure", "LONG"], ["momentum", "SHORT"]], "win_rate": 0.6}
    assert definition_from_rule(balanced) is None
    absent_only = {"conditions": [["volume", ABSENT]], "win_rate": 0.6}
    assert definition_from_rule(absent_only) is None


# ── Registry CRUD + persistence ──────────────────────────────────────────────


def test_registry_register_query_persist_reload():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp)
    d = _def([("structure", "LONG"), ("liquidity", "LONG")])
    assert reg.register(d) is True
    assert reg.register(d) is False  # idempotent on name
    rec = reg.get(d.name)
    assert rec is not None and rec.mode == ModuleMode.SHADOW
    assert d.name.startswith(SYNTH_PREFIX)
    reg.close()

    # Reload from disk — SHADOW survives.
    reg2 = _registry(tmp)
    rec2 = reg2.get(d.name)
    assert rec2 is not None and rec2.mode == ModuleMode.SHADOW
    reg2.close()


# ── compute_votes: shadow vs active influence ────────────────────────────────


def test_shadow_module_does_not_influence_consensus():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp)
    d = _def([("structure", "LONG"), ("liquidity", "LONG")])  # fires on real panel
    reg.register(d)  # SHADOW
    real = _real_votes()
    active, shadow = reg.compute_votes(real)
    assert active == []                  # shadow → nothing joins the panel
    assert len(shadow) == 1 and shadow[0].weight == 0.0
    # Consensus on real+active is identical to real-only (zero regression).
    th = dict(min_net_score=0.5, min_agreement=0.4, high_authority_modules=[],
              high_authority_oppose_confidence=0.6, min_contributors=1)
    base = decide(real, **th)
    with_v = decide(real + active, **th)
    assert base.direction == with_v.direction
    assert abs(base.net_score - with_v.net_score) < 1e-9
    reg.close()


def test_active_module_influences_consensus():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp)
    d = _def([("structure", "LONG"), ("liquidity", "LONG")], direction="LONG", conf=0.9)
    reg.register(d)
    reg.promote(d.name, weight=5.0, total_trades=100, reason="test")
    real = _real_votes()
    active, shadow = reg.compute_votes(real)
    assert len(active) == 1 and active[0].weight == 5.0 and active[0].direction == "LONG"
    assert shadow == []
    th = dict(min_net_score=0.5, min_agreement=0.4, high_authority_modules=[],
              high_authority_oppose_confidence=0.6, min_contributors=1)
    with_v = decide(real + active, **th)
    base = decide(real, **th)
    # The active LONG vote with weight 5 pushes the net more positive.
    assert with_v.net_score > base.net_score
    reg.close()


def test_kill_switch_forces_zero_weight():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp, enabled=False)
    d = _def([("structure", "LONG"), ("liquidity", "LONG")])
    reg.register(d)
    reg.promote(d.name, weight=5.0, total_trades=100)  # ACTIVE in store...
    active, shadow = reg.compute_votes(_real_votes())
    # ...but the kill switch forces it into the shadow bucket (weight 0).
    assert active == []
    assert len(shadow) == 1 and shadow[0].weight == 0.0
    reg.close()


def test_restart_shadow_holds_active_until_trades_elapse():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp, restart=10)
    d = _def([("structure", "LONG"), ("liquidity", "LONG")])
    reg.register(d)
    reg.promote(d.name, weight=4.0, total_trades=100)
    reg.close()

    # Reload — ACTIVE re-enters restart-shadow → weight 0 until 10 trades pass.
    reg2 = _registry(tmp, restart=10)
    active, shadow = reg2.compute_votes(_real_votes())
    assert active == [] and len(shadow) == 1   # held in shadow
    reg2.tick_restart_shadow(200)   # baseline set
    active, _ = reg2.compute_votes(_real_votes())
    assert active == []             # not enough elapsed yet
    reg2.tick_restart_shadow(215)   # 15 >= 10 → cleared
    active, shadow = reg2.compute_votes(_real_votes())
    assert len(active) == 1 and active[0].weight == 4.0
    reg2.close()


def test_no_cascade_virtual_vote_not_input_to_another_virtual():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp)
    a = _def([("structure", "LONG"), ("liquidity", "LONG")], conf=0.9)
    reg.register(a)
    reg.promote(a.name, weight=5.0, total_trades=100)
    # B requires module A's *synthetic* name to vote LONG.
    b = _def([(a.name, "LONG")], conf=0.8)
    reg.register(b)
    reg.promote(b.name, weight=5.0, total_trades=100)
    active, _ = reg.compute_votes(_real_votes())
    names = {v.module for v in active}
    assert a.name in names        # A fires from real votes
    assert b.name not in names    # B never sees A's vote → cannot fire (no cascade)
    reg.close()


def test_empty_registry_is_zero_regression():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp)
    active, shadow = reg.compute_votes(_real_votes())
    assert active == [] and shadow == []
    reg.close()


def test_disabled_module_not_computed():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp)
    d = _def([("structure", "LONG"), ("liquidity", "LONG")])
    reg.register(d)
    reg.promote(d.name, weight=4.0, total_trades=10)
    reg.disable(d.name, reason="bad")
    active, shadow = reg.compute_votes(_real_votes())
    assert active == [] and shadow == []   # retired → no vote at all
    reg.close()


# ── Lifecycle manager: promote / disable / retire ────────────────────────────


def _manager(tmp, cfg, *, feedback=None, cf=None, discovery=None, enabled=True):
    reg = _registry(tmp)
    mgr = VirtualSignalManager(
        reg, cfg, signal_discovery=discovery,
        emitter_feedback=feedback, counterfactual=cf,
    )
    return reg, mgr


def test_ingest_registers_qualifying_rules_as_shadow():
    tmp = tempfile.mkdtemp()
    rules = [
        {"label": "r1", "conditions": [["structure", "LONG"], ["liquidity", "LONG"]],
         "win_rate": 0.6, "qualifies": True, "active": True},
        {"label": "r2", "conditions": [["momentum", "SHORT"]],
         "win_rate": 0.6, "qualifies": True, "active": False},  # not active → skip
    ]
    reg, mgr = _manager(tmp, _cfg(), discovery=_FakeDiscovery(rules))
    res = mgr.evaluate(total_trades=5)
    assert len(res.registered) == 1
    shadows = reg.get_by_mode(ModuleMode.SHADOW)
    assert len(shadows) == 1 and shadows[0].mode == ModuleMode.SHADOW
    reg.close()


def test_promote_when_accuracy_clears_bar():
    tmp = tempfile.mkdtemp()
    d = _def([("structure", "LONG"), ("liquidity", "LONG")])
    rules = [{"label": d.source_label, "conditions": [list(c) for c in d.conditions],
              "win_rate": 0.7, "qualifies": True, "active": True}]
    feedback = _FakeFeedback({d.name: (0.70, 40)})
    reg, mgr = _manager(tmp, _cfg(), feedback=feedback, discovery=_FakeDiscovery(rules))
    mgr.evaluate(total_trades=5)             # registers shadow
    res = mgr.evaluate(total_trades=100)     # enough trades elapsed → promote
    assert d.name in res.promoted
    assert reg.get(d.name).mode == ModuleMode.ACTIVE
    assert reg.get(d.name).weight == 1.0
    reg.close()


def test_no_promote_when_accuracy_below_bar():
    tmp = tempfile.mkdtemp()
    d = _def([("structure", "LONG"), ("liquidity", "LONG")])
    rules = [{"label": d.source_label, "conditions": [list(c) for c in d.conditions],
              "win_rate": 0.7, "qualifies": True, "active": True}]
    feedback = _FakeFeedback({d.name: (0.40, 40)})   # below min_shadow_accuracy
    reg, mgr = _manager(tmp, _cfg(), feedback=feedback, discovery=_FakeDiscovery(rules))
    mgr.evaluate(total_trades=5)
    res = mgr.evaluate(total_trades=100)
    assert res.promoted == []
    assert reg.get(d.name).mode == ModuleMode.SHADOW
    reg.close()


def test_max_active_cap_blocks_new_promotions():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp, max_active=1)
    cfg = _cfg(max_promotions_per_cycle=5)
    # Pre-fill one ACTIVE module to hit the cap.
    a = _def([("structure", "LONG")], conf=0.9)
    reg.register(a)
    reg.promote(a.name, weight=1.0, total_trades=0)
    b = _def([("liquidity", "LONG")], conf=0.9)
    reg.register(b)  # SHADOW, eligible by accuracy
    feedback = _FakeFeedback({b.name: (0.9, 40)})
    mgr = VirtualSignalManager(reg, cfg, emitter_feedback=feedback)
    res = mgr.evaluate(total_trades=100)
    assert b.name not in res.promoted          # cap (1 active) blocks promotion
    assert reg.get(b.name).mode == ModuleMode.SHADOW
    reg.close()


def test_max_promotions_per_cycle_rate_limit():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp, max_active=10)
    cfg = _cfg(max_promotions_per_cycle=1)
    feedback_table = {}
    for i in range(3):
        d = _def([(f"m{i}", "LONG")], conf=0.9)
        reg.register(d, total_trades=0)
        feedback_table[d.name] = (0.9, 40)
    mgr = VirtualSignalManager(reg, cfg, emitter_feedback=_FakeFeedback(feedback_table))
    res = mgr.evaluate(total_trades=100)
    assert len(res.promoted) == 1              # only one per cycle
    reg.close()


def test_retire_degraded_active_by_accuracy():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp)
    d = _def([("structure", "LONG")], conf=0.9)
    reg.register(d)
    reg.promote(d.name, weight=1.0, total_trades=10)
    feedback = _FakeFeedback({d.name: (0.30, 40)})  # below retirement threshold
    mgr = VirtualSignalManager(reg, _cfg(), emitter_feedback=feedback)
    res = mgr.evaluate(total_trades=200)
    assert d.name in res.retired
    assert reg.get(d.name).mode == ModuleMode.DISABLED
    reg.close()


def test_retire_active_by_harmful_marginal_r():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp)
    d = _def([("structure", "LONG")], conf=0.9)
    reg.register(d)
    reg.promote(d.name, weight=1.0, total_trades=10)
    feedback = _FakeFeedback({d.name: (0.70, 40)})   # accuracy fine
    cf = _FakeCF([{
        "module": d.name, "trades_involved": 80, "marginal_r": -8.0,
        "better_off_without": True,
    }])  # mr_per_trade = -0.1 < -0.05 threshold
    mgr = VirtualSignalManager(reg, _cfg(), emitter_feedback=feedback, counterfactual=cf)
    res = mgr.evaluate(total_trades=200)
    assert d.name in res.retired
    reg.close()


def test_promotion_disabled_still_ticks_restart_shadow():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp, restart=5)
    d = _def([("structure", "LONG")], conf=0.9)
    reg.register(d)
    reg.promote(d.name, weight=2.0, total_trades=0)
    reg.close()
    # Reload (active → restart shadow), promotion OFF.
    reg2 = _registry(tmp, restart=5)
    cfg = _cfg(virtual_promotion_enabled=False)
    mgr = VirtualSignalManager(reg2, cfg)
    mgr.evaluate(total_trades=100)   # baseline
    mgr.evaluate(total_trades=110)   # 10 >= 5 → cleared
    active, _ = reg2.compute_votes(_real_votes())
    assert len(active) == 1          # restart-shadow cleared even with promotion off
    reg2.close()


# ── TunerAgent guard + adapter ───────────────────────────────────────────────


def test_evaluate_blocked_when_agent_sole_authority():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp)
    rules = [{"label": "r", "conditions": [["structure", "LONG"]],
              "win_rate": 0.7, "qualifies": True, "active": True}]
    mgr = VirtualSignalManager(reg, _cfg(), signal_discovery=_FakeDiscovery(rules))
    agent = TunerAgent(enabled=True, audit_db_path=os.path.join(tmp, "audit.db"))
    mgr.set_tuner_agent(agent)
    # Direct call is blocked (agent is sole authority, not authorizing).
    res = mgr.evaluate(total_trades=100)
    assert res.registered == [] and "blocked" in res.reason
    reg.close()


def test_tunable_adapter_drives_lifecycle_through_agent():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp)
    d = _def([("structure", "LONG"), ("liquidity", "LONG")])
    rules = [{"label": d.source_label, "conditions": [list(c) for c in d.conditions],
              "win_rate": 0.7, "qualifies": True, "active": True}]
    feedback = _FakeFeedback({d.name: (0.7, 40)})
    mgr = VirtualSignalManager(reg, _cfg(), emitter_feedback=feedback,
                               signal_discovery=_FakeDiscovery(rules))
    agent = TunerAgent(enabled=True, audit_db_path=os.path.join(tmp, "audit.db"))
    mgr.set_tuner_agent(agent)
    adapter = VirtualSignalManagerTunable(mgr, min_trades=0)
    agent.register(adapter)
    # Agent-driven (authorised) force run registers the shadow candidate.
    ctx = TuneContext(total_trades=5, force=True)
    results = agent.force_tune("virtual_signal_manager", ctx)
    assert results is not None and results.success
    assert reg.get(d.name) is not None   # registered via the agent path
    reg.close()


def test_manager_syncs_kill_switch_from_config():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp, enabled=True)
    d = _def([("structure", "LONG")], conf=0.9)
    reg.register(d)
    reg.promote(d.name, weight=3.0, total_trades=0)
    # Config flips the discovery kill switch OFF — the manager syncs it on
    # evaluate so the active module's live weight drops to 0.
    cfg = _cfg(signal_discovery_enabled=False)
    mgr = VirtualSignalManager(reg, cfg)
    assert reg.enabled is True
    mgr.evaluate(total_trades=100)
    assert reg.enabled is False
    active, shadow = reg.compute_votes(_real_votes())
    assert active == [] and len(shadow) == 1   # forced to weight 0
    reg.close()


def test_status_shapes_for_dashboard():
    tmp = tempfile.mkdtemp()
    reg = _registry(tmp)
    d = _def([("structure", "LONG")], conf=0.9)
    reg.register(d)
    mgr = VirtualSignalManager(reg, _cfg())
    status = mgr.get_status()
    assert "modules" in status and "counts" in status
    assert status["counts"]["SHADOW"] == 1
    assert status["promotion_enabled"] is True
    reg.close()


# ── Scanner integration (deterministic stub registry) ───────────────────────


class _StubRegistry:
    """Duck-typed registry returning fixed votes — isolates the scanner-side
    injection wiring from the (separately unit-tested) firing logic."""

    def __init__(self, active, shadow):
        self._active = active
        self._shadow = shadow

    def compute_votes(self, real_votes):
        return list(self._active), list(self._shadow)


def _real_dataframes_available() -> bool:
    """Several other suite modules replace sys.modules['numpy'/'pandas'] with
    minimal stubs at import time and never restore them, which would break the
    heavy scanner-integration tests below when collected after them. Detect that
    so those two tests skip (rather than spuriously fail) under the polluted
    full-suite ordering; they run normally in isolation / a clean process."""
    return hasattr(_pd, "date_range") and hasattr(_np, "full") and hasattr(_np, "random")


def _scan_dfs():
    from datetime import datetime, timezone

    def df(n, base=1.27):
        idx = _pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
        price = base + _np.cumsum(_np.random.RandomState(1).normal(0, 0.0002, n))
        return _pd.DataFrame({
            "open": price, "high": price + 0.0003, "low": price - 0.0003,
            "close": price, "volume": _np.full(n, 1000.0),
        }, index=idx)

    return df(100), df(200), df(300), df(500), datetime(2024, 1, 2, 12, tzinfo=timezone.utc)


def test_scanner_injects_active_and_records_shadow():
    if not _real_dataframes_available():
        pytest.skip("numpy/pandas stubbed by another suite module — run in isolation")
    from scanner.pair_scanner import PairScanner

    scanner = PairScanner()
    active = [Vote("synth__active", "LONG", 0.85, 2.0,
                   evidence={"virtual": True, "mode": "ACTIVE"})]
    shadow = [Vote("synth__shadow", "SHORT", 0.7, 0.0,
                   evidence={"virtual": True, "mode": "SHADOW"})]
    scanner.set_virtual_registry(_StubRegistry(active, shadow))

    h4, h1, m15, m5, utc = _scan_dfs()
    result = scanner.scan_pair("EURUSD", h4, h1, m15, m5, utc_now=utc)

    vote_modules = {getattr(v, "module", "") for v in result.votes}
    shadow_modules = {getattr(v, "module", "") for v in result.shadow_votes}
    # ACTIVE synthetic vote joined the live decision panel.
    assert "synth__active" in vote_modules
    # SHADOW synthetic vote recorded separately — never in the decision panel.
    assert "synth__shadow" in shadow_modules
    assert "synth__shadow" not in vote_modules


def test_scanner_no_registry_is_zero_regression():
    if not _real_dataframes_available():
        pytest.skip("numpy/pandas stubbed by another suite module — run in isolation")
    from scanner.pair_scanner import PairScanner

    h4, h1, m15, m5, utc = _scan_dfs()
    base = PairScanner().scan_pair("EURUSD", h4, h1, m15, m5, utc_now=utc)

    scanner = PairScanner()
    scanner.set_virtual_registry(None)
    same = scanner.scan_pair("EURUSD", h4, h1, m15, m5, utc_now=utc)

    # No registry → no synthetic votes, identical direction + vote count.
    assert same.shadow_votes == []
    assert base.direction == same.direction
    assert len(base.votes) == len(same.votes)

"""
Phase 9 — Upstream kill-gate → bounded dimmer.

The serial entry pipeline historically *killed* any setup that fell short of a
hard QUALITY threshold (scanner OQ/EQ/score, the entry-time OQ/EQ revalidation,
the planner conviction floor, the entry-score floor). With the orchestrator as
the live sizer those quality gates instead *dim* the trade: a near-miss flows
through carrying a bounded quality multiplier the orchestrator folds into size,
so it trades SMALL instead of dying — only a truly hopeless setup (caught by a
separate hard SAFETY floor) is still killed.

These tests cover the pure decision functions and the orchestrator's folding,
the safety floors, the "softening disabled / orchestrator off" legacy paths, and
that the three multipliers compound correctly into the graded size.
"""

import pytest

from brain.orchestrator import (
    Orchestrator,
    TradeProposal,
    gate_quality_multiplier,
)
from scanner.pair_scanner import soften_scanner_gate
from planning import PlannerConfig, TradePlanContext, TradePlanner


# ── Duck-typed configs ─────────────────────────────────────────────────────

class _OrchCfg:
    """Minimal duck-typed OrchestratorConfig with softening ON."""
    enabled = True
    apply_sizing = True
    size_floor = 0.5
    dimension_floor = 0.6
    ranker_ev_full = 1.5
    de_margin_full = 0.5
    scan_score_full = 100.0
    scalp_htf_opposition_scale = 0.3
    # Phase 9
    soften_scanner_gates = True
    soften_planner_gates = True
    soften_entry_gates = True
    gate_quality_floor = 0.15
    scanner_safety_oq = 2.0
    scanner_safety_eq = 2.0
    scanner_safety_score = 50.0
    entry_safety_score = 40.0


class _LdCfg:
    enabled = True
    opportunity_quality_min = 5.0
    entry_quality_min = 5.0
    ready_min_score = 85


# ── The shared multiplier helper ───────────────────────────────────────────

class TestGateQualityMultiplier:
    def test_single_dimension_ratio(self):
        # 0.40 vs 0.50 → 0.80
        assert gate_quality_multiplier([(0.40, 0.50)]) == pytest.approx(0.80)

    def test_dimensions_compound(self):
        # (4/5) × (3/5) = 0.48
        assert gate_quality_multiplier([(4, 5), (3, 5)]) == pytest.approx(0.48)

    def test_at_or_above_threshold_is_full(self):
        assert gate_quality_multiplier([(6, 5)]) == 1.0
        assert gate_quality_multiplier([(5, 5)]) == 1.0

    def test_floor_clamps_zero(self):
        assert gate_quality_multiplier([(0.0, 0.5)], floor=0.15) == pytest.approx(0.15)

    def test_never_exceeds_one(self):
        assert gate_quality_multiplier([(100, 1)]) == 1.0

    def test_zero_threshold_ignored(self):
        assert gate_quality_multiplier([(0.0, 0.0)]) == 1.0


# ── Gate #3: scanner READY gate ────────────────────────────────────────────

class TestScannerGateSoftening:
    def test_below_threshold_flows_with_multiplier(self):
        # OQ/EQ below the READY floor (5) but above the safety floor (2);
        # score above safety (50) → promoted to READY with a multiplier < 1.
        status, mult = soften_scanner_gate(
            "WATCHLIST", "LONG", oq_score=4.0, eq_score=4.5, score=70,
            ld_cfg=_LdCfg(), orch=_OrchCfg(),
        )
        assert status == "READY"
        assert 0.15 <= mult < 1.0

    def test_way_below_threshold_still_killed(self):
        # OQ below the hard safety floor (2.0) → NOT softened, stays WAITING.
        status, mult = soften_scanner_gate(
            "WAITING", "LONG", oq_score=1.0, eq_score=4.0, score=70,
            ld_cfg=_LdCfg(), orch=_OrchCfg(),
        )
        assert status == "WAITING"
        assert mult == 1.0

    def test_score_below_safety_still_killed(self):
        status, mult = soften_scanner_gate(
            "WATCHLIST", "LONG", oq_score=4.0, eq_score=4.0, score=40,
            ld_cfg=_LdCfg(), orch=_OrchCfg(),
        )
        assert status == "WATCHLIST"
        assert mult == 1.0

    def test_softening_disabled_legacy(self):
        cfg = _OrchCfg()
        cfg.soften_scanner_gates = False
        status, mult = soften_scanner_gate(
            "WATCHLIST", "LONG", oq_score=4.0, eq_score=4.0, score=70,
            ld_cfg=_LdCfg(), orch=cfg,
        )
        assert status == "WATCHLIST"
        assert mult == 1.0

    def test_orchestrator_disabled_legacy(self):
        cfg = _OrchCfg()
        cfg.enabled = False
        status, mult = soften_scanner_gate(
            "WATCHLIST", "LONG", oq_score=4.0, eq_score=4.0, score=70,
            ld_cfg=_LdCfg(), orch=cfg,
        )
        assert status == "WATCHLIST"
        assert mult == 1.0

    def test_no_orchestrator_legacy(self):
        status, mult = soften_scanner_gate(
            "WATCHLIST", "LONG", 4.0, 4.0, 70, _LdCfg(), None,
        )
        assert status == "WATCHLIST"
        assert mult == 1.0

    def test_ready_setup_untouched(self):
        # An already-READY setup is never re-graded — full quality.
        status, mult = soften_scanner_gate(
            "READY", "LONG", 8.0, 8.0, 95, _LdCfg(), _OrchCfg(),
        )
        assert status == "READY"
        assert mult == 1.0

    def test_neutral_direction_not_softened(self):
        status, mult = soften_scanner_gate(
            "WAITING", "NEUTRAL", 4.0, 4.0, 70, _LdCfg(), _OrchCfg(),
        )
        assert status == "WAITING"
        assert mult == 1.0


# ── Gate #9: planner conviction gate ───────────────────────────────────────

def _low_conviction_ctx(**overrides) -> TradePlanContext:
    base = dict(
        symbol="EURUSD",
        pip_size=0.0001,
        atr_pips=20.0,
        current_price=1.1000,
        direction="LONG",
        scanner_score=55.0,
        zone_type="ORDER_BLOCK",
        zone_quality=0.2,
        zone_entry_price=1.0998,
        de_confidence=0.2,
        de_tf_alignment=-0.5,
        rl_action=2,            # RL says SELL against our LONG
        rl_confidence=0.5,
        rl_expected_r=1.0,
        pair_win_rate=0.4,
        session_win_rate=0.6,
        proposed_sl_price=1.0980,
        proposed_sl_pips=20.0,
        structure_sl_available=True,
        risk_reward_2=3.0,
        base_risk_pct=0.5,
        session="LONDON",
        situation_label="RANGE",
    )
    base.update(overrides)
    return TradePlanContext(**base)


class TestPlannerGateSoftening:
    def test_legacy_low_conviction_skips(self):
        cfg = PlannerConfig(soften_gates=False)
        plan = TradePlanner(cfg).plan_trade(_low_conviction_ctx())
        assert plan.action == "SKIP"
        assert plan.gate_quality_multiplier == 1.0

    def test_softened_low_conviction_enters_with_multiplier(self):
        cfg = PlannerConfig(soften_gates=True, gate_quality_floor=0.15)
        plan = TradePlanner(cfg).plan_trade(_low_conviction_ctx())
        # No governor attached → conviction gate was the only blocker.
        assert plan.action == "ENTER"
        assert 0.15 <= plan.gate_quality_multiplier < 1.0

    def test_softened_full_conviction_unchanged(self):
        cfg = PlannerConfig(soften_gates=True)
        ctx = _low_conviction_ctx(
            scanner_score=85.0, de_confidence=0.8, de_tf_alignment=0.8,
            zone_quality=0.85, pair_win_rate=0.6,
        )
        plan = TradePlanner(cfg).plan_trade(ctx)
        assert plan.action == "ENTER"
        assert plan.gate_quality_multiplier == 1.0


# ── Orchestrator: folding the three multipliers into size ──────────────────

class TestOrchestratorGateFolding:
    @pytest.fixture
    def orch(self):
        return Orchestrator(_OrchCfg())

    def _full_proposal(self, **overrides):
        base = dict(
            pair="EURUSD", direction="LONG", horizon="SCALP",
            ranker_ev=1.5, ranker_coherence=1.0, ranker_confidence=1.0,
            tf_alignment=0.5, de_margin=0.5, de_conviction=1.0,
            advisor_agreement=1.0, scan_score=100.0,
        )
        base.update(overrides)
        return TradeProposal(**base)

    def test_clean_setup_has_no_gate_dimensions(self, orch):
        v = orch.evaluate(self._full_proposal())
        names = [d.name for d in v.dimensions]
        assert "scanner_gate" not in names
        assert "planner_gate" not in names
        assert "entry_gate" not in names
        assert v.size_multiplier == pytest.approx(1.0)

    def test_single_gate_multiplier_dims_size(self, orch):
        clean = orch.evaluate(self._full_proposal()).size_multiplier
        v = orch.evaluate(self._full_proposal(gate_quality_multiplier=0.5))
        assert v.size_multiplier < clean
        names = [d.name for d in v.dimensions]
        assert "scanner_gate" in names

    def test_softened_setup_sizes_below_analytic_floor(self, orch):
        # All analytic dims full (→ product 1.0, floored at size_floor 0.5),
        # but a heavy gate multiplier must pull the size BELOW size_floor.
        v = orch.evaluate(self._full_proposal(gate_quality_multiplier=0.2))
        assert v.size_multiplier < 0.5
        assert v.size_multiplier >= _OrchCfg.gate_quality_floor - 1e-9

    def test_three_multipliers_compound(self, orch):
        v = orch.evaluate(self._full_proposal(
            gate_quality_multiplier=0.8,
            planner_quality_multiplier=0.8,
            entry_quality_multiplier=0.8,
        ))
        # 0.8^3 = 0.512, above the gate floor → not clamped.
        assert v.size_multiplier == pytest.approx(0.512, abs=1e-3)
        names = [d.name for d in v.dimensions]
        assert {"scanner_gate", "planner_gate", "entry_gate"} <= set(names)

    def test_gate_floor_clamps_extreme(self, orch):
        v = orch.evaluate(self._full_proposal(
            gate_quality_multiplier=0.15,
            planner_quality_multiplier=0.15,
            entry_quality_multiplier=0.15,
        ))
        assert v.size_multiplier == pytest.approx(_OrchCfg.gate_quality_floor)

    def test_physics_veto_overrides_gate_multipliers(self, orch):
        from brain.orchestrator import VETO_NEGATIVE_MARGIN
        v = orch.evaluate(self._full_proposal(
            gate_quality_multiplier=0.9,
            physics_vetoes=[VETO_NEGATIVE_MARGIN],
        ))
        assert v.vetoed is True
        assert v.size_multiplier == 0.0


# ── Gate #10: entry-score gate helper ──────────────────────────────────────

class TestEntryGateSoftening:
    def _engine(self, orch_overrides=None):
        from config import AppConfig
        from trigger.entry_engine import EntryEngine
        cfg = AppConfig()
        if orch_overrides:
            for k, val in orch_overrides.items():
                setattr(cfg.orchestrator, k, val)
        return EntryEngine(cfg)

    def test_softening_active_returns_config(self):
        eng = self._engine({"enabled": True, "soften_entry_gates": True})
        assert eng._entry_gate_softening() is not None

    def test_softening_disabled_returns_none(self):
        eng = self._engine({"enabled": True, "soften_entry_gates": False})
        assert eng._entry_gate_softening() is None

    def test_orchestrator_disabled_returns_none(self):
        eng = self._engine({"enabled": False, "soften_entry_gates": True})
        assert eng._entry_gate_softening() is None

    def test_entry_signal_carries_multiplier_field(self):
        from trigger.entry_engine import EntrySignal
        sig = EntrySignal(
            pair="EURUSD", direction="LONG", entry_type="OB_MIDPOINT",
            entry_price=1.1, stop_loss=1.09, tp1=1.11, tp2=1.12,
            risk_reward_1=1.0, risk_reward_2=2.0, risk_pips=10.0,
            position_size_lots=0.1, score=80,
        )
        assert sig.entry_quality_multiplier == 1.0


# ── Config defaults ────────────────────────────────────────────────────────

class TestConfigDefaults:
    def test_softening_defaults_on_with_safety_floors(self):
        from config import OrchestratorConfig
        c = OrchestratorConfig()
        assert c.soften_scanner_gates is True
        assert c.soften_planner_gates is True
        assert c.soften_entry_gates is True
        assert 0.0 < c.gate_quality_floor < 1.0
        assert c.scanner_safety_oq >= 0
        assert c.entry_safety_score >= 0

    def test_invalid_gate_floor_rejected(self):
        from config import OrchestratorConfig
        with pytest.raises(ValueError):
            OrchestratorConfig(gate_quality_floor=1.5)

    def test_invalid_safety_floor_rejected(self):
        from config import OrchestratorConfig
        with pytest.raises(ValueError):
            OrchestratorConfig(scanner_safety_oq=-1.0)

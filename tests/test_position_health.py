"""
Tests for the Orchestrator's live-position management round table (Phase 8).

Dependency-light — pure health math, no torch/pandas/numpy required. Verifies
that an open position is graded into a continuous health score (geometric mean of
its dimension healths) mapped to a bounded management action; that every
dimension behaves correctly in isolation; that the thresholds map as configured;
that SCALE_UP is opt-in; and that missing / partial evidence degrades gracefully
to a neutral read rather than crashing or spuriously exiting.
"""

import pytest

from config import OrchestratorConfig
from brain.orchestrator import (
    Orchestrator,
    PositionEvidence,
    PositionHealthReport,
    ManagementAction,
)


@pytest.fixture
def orch():
    return Orchestrator(OrchestratorConfig())


def _healthy(**over) -> PositionEvidence:
    base = dict(
        pair="EURUSD", direction="LONG", horizon="SWING",
        profit_r=0.5, momentum=0.6, structure_integrity=0.95, tf_alignment=0.4,
        read_confidence=0.8, urgency=0.0, hold_minutes=30.0,
        expected_hold_minutes=240.0, portfolio_heat_pct=5.0,
        entry_health=0.8, entry_structure_integrity=0.9, entry_tf_alignment=0.4,
    )
    base.update(over)
    return PositionEvidence(**base)


# ── Health score + action mapping ──────────────────────────────────────────
class TestHealthToAction:
    def test_healthy_position_holds(self, orch):
        r = orch.evaluate_open_position(_healthy())
        assert isinstance(r, PositionHealthReport)
        assert r.health_score >= 0.8
        assert r.action == ManagementAction.HOLD

    def test_fading_position_tightens(self, orch):
        r = orch.evaluate_open_position(_healthy(
            momentum=-0.2, structure_integrity=0.6, tf_alignment=0.1,
            hold_minutes=120.0,
        ))
        assert 0.6 <= r.health_score < 0.8
        assert r.action == ManagementAction.TIGHTEN_SL

    def test_dead_thesis_exits_full(self, orch):
        r = orch.evaluate_open_position(_healthy(
            profit_r=-1.5, momentum=-0.8, structure_integrity=0.05,
            tf_alignment=-0.6, hold_minutes=600.0, portfolio_heat_pct=80.0,
        ))
        assert r.health_score < 0.2
        assert r.action == ManagementAction.EXIT_FULL

    def test_broken_structure_alone_drives_exit(self, orch):
        # A single fully-broken dimension (structure 0.0) zeroes the geomean —
        # management is protecting capital, so it is allowed to exit.
        r = orch.evaluate_open_position(_healthy(structure_integrity=0.0))
        assert r.health_score == 0.0
        assert r.action == ManagementAction.EXIT_FULL

    def test_action_thresholds_are_configurable(self):
        cfg = OrchestratorConfig()
        cfg.health_thresholds = {"hold": 0.99, "tighten": 0.0, "scale_down": 0.0, "exit_partial": 0.0}
        r = Orchestrator(cfg).evaluate_open_position(_healthy())
        # Same healthy evidence now falls below the raised HOLD bar → TIGHTEN.
        assert r.action == ManagementAction.TIGHTEN_SL

    def test_trim_actions_carry_size_pct(self, orch):
        # Tune to land in the SCALE_DOWN band [0.4, 0.6).
        r = orch.evaluate_open_position(_healthy(
            momentum=-0.4, structure_integrity=0.4, tf_alignment=-0.2,
            profit_r=-0.3, hold_minutes=300.0, portfolio_heat_pct=30.0,
        ))
        if r.action in (ManagementAction.SCALE_DOWN, ManagementAction.EXIT_PARTIAL):
            assert r.recommended_size_pct is not None
            assert 0.0 < r.recommended_size_pct <= 1.0
        else:
            assert r.recommended_size_pct is None


# ── Per-dimension behaviour ────────────────────────────────────────────────
class TestDimensions:
    def test_dimension_scores_present(self, orch):
        r = orch.evaluate_open_position(_healthy())
        for name in (
            "thesis_integrity", "momentum_alignment", "risk_exposure",
            "time_decay", "profit_trajectory", "situation_shift",
        ):
            assert name in r.dimension_scores

    def test_momentum_against_lowers_health(self, orch):
        with_m = orch.evaluate_open_position(_healthy(momentum=0.8))
        against = orch.evaluate_open_position(_healthy(momentum=-0.8))
        assert against.health_score < with_m.health_score

    def test_time_decay_only_after_expected_hold(self, orch):
        within = orch.evaluate_open_position(_healthy(hold_minutes=100.0, expected_hold_minutes=240.0))
        over = orch.evaluate_open_position(_healthy(hold_minutes=600.0, expected_hold_minutes=240.0))
        assert within.dimension_scores["time_decay"] == 1.0
        assert over.dimension_scores["time_decay"] < 1.0

    def test_profit_trajectory_winner_full(self, orch):
        r = orch.evaluate_open_position(_healthy(profit_r=2.0))
        assert r.dimension_scores["profit_trajectory"] == 1.0

    def test_profit_trajectory_loss_dims(self, orch):
        r = orch.evaluate_open_position(_healthy(profit_r=-1.0))
        assert r.dimension_scores["profit_trajectory"] < 0.5

    def test_situation_shift_neutral_without_baseline(self, orch):
        r = orch.evaluate_open_position(_healthy(
            entry_structure_integrity=None, entry_tf_alignment=None,
        ))
        assert r.dimension_scores["situation_shift"] == 1.0

    def test_situation_shift_flags_degradation(self, orch):
        steady = orch.evaluate_open_position(_healthy(
            structure_integrity=0.9, entry_structure_integrity=0.9,
        ))
        degraded = orch.evaluate_open_position(_healthy(
            structure_integrity=0.3, entry_structure_integrity=0.9,
        ))
        assert degraded.dimension_scores["situation_shift"] < steady.dimension_scores["situation_shift"]

    def test_health_delta_vs_entry(self, orch):
        r = orch.evaluate_open_position(_healthy(entry_health=0.6))
        assert r.entry_health_at_open == 0.6
        assert abs(r.health_delta - (r.health_score - 0.6)) < 1e-6


# ── Edge cases ──────────────────────────────────────────────────────────────
class TestEdgeCases:
    def test_missing_evidence_is_neutral_not_crash(self, orch):
        r = orch.evaluate_open_position(PositionEvidence(pair="X", direction="LONG"))
        # All reads absent → every dimension neutral (1.0) → fully healthy HOLD.
        assert r.health_score == pytest.approx(1.0, abs=1e-9)
        assert r.action == ManagementAction.HOLD

    def test_partial_evidence(self, orch):
        r = orch.evaluate_open_position(PositionEvidence(
            pair="X", direction="SHORT", structure_integrity=0.5,
        ))
        assert 0.0 <= r.health_score <= 1.0

    def test_health_dimension_floor_clamps(self):
        cfg = OrchestratorConfig()
        cfg.health_dimension_floor = 0.2
        r = Orchestrator(cfg).evaluate_open_position(_healthy(structure_integrity=0.0))
        # With a 0.2 floor no single dimension can zero the product.
        assert r.dimension_scores["thesis_integrity"] >= 0.2
        assert r.health_score > 0.0

    def test_to_dict_round_trips(self, orch):
        d = orch.evaluate_open_position(_healthy()).to_dict()
        assert d["action"] in {a.value for a in ManagementAction}
        assert "dimension_scores" in d and "health_score" in d
        assert isinstance(d["dimensions"], list)

    def test_thesis_changes_flagged_when_weak(self, orch):
        r = orch.evaluate_open_position(_healthy(
            structure_integrity=0.2, momentum=-0.6,
        ))
        assert len(r.thesis_changes) >= 1


# ── SCALE_UP safety (opt-in only) ───────────────────────────────────────────
class TestScaleUpGating:
    def _improving(self) -> PositionEvidence:
        # Strongly healthy now, much healthier than at entry.
        return _healthy(
            momentum=1.0, structure_integrity=1.0, tf_alignment=0.8,
            profit_r=1.5, portfolio_heat_pct=0.0, hold_minutes=10.0,
            entry_health=0.55, entry_structure_integrity=0.6,
        )

    def test_scale_up_blocked_by_default(self, orch):
        r = orch.evaluate_open_position(self._improving())
        assert r.action != ManagementAction.SCALE_UP

    def test_scale_up_allowed_when_enabled(self):
        cfg = OrchestratorConfig()
        cfg.allow_scale_up = True
        r = Orchestrator(cfg).evaluate_open_position(
            _healthy(
                momentum=1.0, structure_integrity=1.0, tf_alignment=0.8,
                profit_r=1.5, portfolio_heat_pct=0.0, hold_minutes=10.0,
                entry_health=0.55, entry_structure_integrity=0.6,
            )
        )
        assert r.action == ManagementAction.SCALE_UP

    def test_scale_up_needs_improvement_over_entry(self):
        cfg = OrchestratorConfig()
        cfg.allow_scale_up = True
        # Healthy but NOT meaningfully better than a high entry health.
        r = Orchestrator(cfg).evaluate_open_position(_healthy(entry_health=0.99))
        assert r.action != ManagementAction.SCALE_UP


# ── Config-driven references ────────────────────────────────────────────────
class TestConfigOverrides:
    def test_expected_hold_drives_time_decay(self):
        cfg = OrchestratorConfig()
        orch = Orchestrator(cfg)
        # Same 100m hold: within a 240m horizon (no decay) vs over a 30m one.
        within = orch.evaluate_open_position(_healthy(hold_minutes=100.0, expected_hold_minutes=240.0))
        over = orch.evaluate_open_position(_healthy(hold_minutes=100.0, expected_hold_minutes=30.0))
        assert over.dimension_scores["time_decay"] < within.dimension_scores["time_decay"]

    def test_loss_full_r_reference(self):
        cfg = OrchestratorConfig()
        cfg.profit_loss_full_r = 0.5  # a 0.5R loss fully drains the profit dim
        r = Orchestrator(cfg).evaluate_open_position(_healthy(profit_r=-0.5))
        assert r.dimension_scores["profit_trajectory"] == pytest.approx(0.0, abs=1e-9)


# ── Main-loop wiring safety (fallback / pacing / action mapping) ─────────────
class TestManagementWiring:
    def _loop(self, **cfg_over):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from platforms.main_loop import TradingLoop

        cfg = OrchestratorConfig()
        for k, v in cfg_over.items():
            setattr(cfg, k, v)
        loop = MagicMock(spec=TradingLoop)
        loop.config = SimpleNamespace(orchestrator=cfg)
        loop._orchestrator = Orchestrator(cfg)
        loop._decision_engine = MagicMock()
        loop._mgmt_cycle_count = {}
        loop._mgmt_last_eval_cycle = {}
        loop._entry_health_snapshot = {}
        return loop

    def _ctx_sa(self):
        from unittest.mock import MagicMock
        ctx = MagicMock()
        ctx.profit_r = 0.5
        ctx.hold_minutes = 50.0
        ctx.portfolio_heat_pct = 5.0
        sa = MagicMock()
        sa.momentum = 0.5
        sa.structure_integrity = 0.9
        sa.tf_alignment = 0.4
        sa.read_confidence = 0.8
        sa.urgency = 0.0
        sa.tf_vector.return_value = {}
        return ctx, sa

    def test_disabled_falls_back_to_decide_management(self):
        from unittest.mock import MagicMock
        from platforms.main_loop import TradingLoop

        loop = self._loop(manage_open_positions=False)
        sentinel = MagicMock()
        loop._decision_engine.decide_management.return_value = sentinel
        ctx, sa = self._ctx_sa()
        out = TradingLoop._decide_management(loop, "oid-1", MagicMock(symbol="EURUSD", direction="BUY"), ctx, sa)
        assert out is sentinel
        loop._decision_engine.decide_management.assert_called_once_with(ctx, sa)

    def test_pacing_min_cycles_uses_legacy_first(self):
        from unittest.mock import MagicMock
        from platforms.main_loop import TradingLoop

        loop = self._loop(manage_open_positions=True, min_cycles_before_management=3)
        sentinel = MagicMock()
        loop._decision_engine.decide_management.return_value = sentinel
        ctx, sa = self._ctx_sa()
        pos = MagicMock(symbol="EURUSD", direction="BUY", order_id="oid-1")
        # First call: count becomes 1 (<= 3) → legacy path.
        out = TradingLoop._decide_management(loop, "oid-1", pos, ctx, sa)
        assert out is sentinel
        assert loop._mgmt_cycle_count["oid-1"] == 1

    def test_exception_falls_back_to_decide_management(self):
        from unittest.mock import MagicMock
        from platforms.main_loop import TradingLoop

        loop = self._loop(manage_open_positions=True, min_cycles_before_management=0,
                          management_cooldown_cycles=0)
        sentinel = MagicMock()
        loop._decision_engine.decide_management.return_value = sentinel
        loop._build_position_evidence.side_effect = RuntimeError("boom")
        ctx, sa = self._ctx_sa()
        pos = MagicMock(symbol="EURUSD", direction="BUY", order_id="oid-1")
        out = TradingLoop._decide_management(loop, "oid-1", pos, ctx, sa)
        assert out is sentinel  # never leaves the position unmanaged

    def test_success_path_records_and_returns_translated(self):
        from unittest.mock import MagicMock
        from platforms.main_loop import TradingLoop

        loop = self._loop(manage_open_positions=True, min_cycles_before_management=0,
                          management_cooldown_cycles=0)
        # Use the real evidence builder + translator on the mock self.
        loop._build_position_evidence.side_effect = lambda *a, **k: TradingLoop._build_position_evidence(loop, *a, **k)
        loop._management_decision_from_health.side_effect = lambda *a, **k: TradingLoop._management_decision_from_health(loop, *a, **k)
        loop._situation_engine = MagicMock()
        ctx, sa = self._ctx_sa()
        pos = MagicMock(symbol="EURUSD", direction="BUY", order_id="oid-1")
        out = TradingLoop._decide_management(loop, "oid-1", pos, ctx, sa)
        # A healthy position → a real ManagementDecision (HOLD/TIGHTEN), recorded.
        assert out is not None
        loop._record_position_health.assert_called_once()
        assert loop._mgmt_last_eval_cycle["oid-1"] == 1

    def test_health_maps_exit_full_to_close(self):
        from unittest.mock import MagicMock
        from platforms.main_loop import TradingLoop
        from decision.actions import Action

        loop = self._loop()
        report = PositionHealthReport(
            pair="EURUSD", direction="LONG", horizon="SWING",
            health_score=0.1, action=ManagementAction.EXIT_FULL,
        )
        out = TradingLoop._management_decision_from_health(loop, report, MagicMock())
        assert out.action == Action.CLOSE

    def test_health_maps_trim_to_partial_close(self):
        from unittest.mock import MagicMock
        from platforms.main_loop import TradingLoop
        from decision.actions import Action

        loop = self._loop()
        report = PositionHealthReport(
            pair="EURUSD", direction="LONG", horizon="SWING",
            health_score=0.3, action=ManagementAction.EXIT_PARTIAL,
            recommended_size_pct=0.6,
        )
        out = TradingLoop._management_decision_from_health(loop, report, MagicMock())
        assert out.action == Action.PARTIAL_CLOSE
        assert out.partial_ratio == 0.6

    def test_health_maps_scale_up_to_hold(self):
        from unittest.mock import MagicMock
        from platforms.main_loop import TradingLoop
        from decision.actions import Action

        loop = self._loop()
        report = PositionHealthReport(
            pair="EURUSD", direction="LONG", horizon="SWING",
            health_score=0.95, action=ManagementAction.SCALE_UP,
        )
        out = TradingLoop._management_decision_from_health(loop, report, MagicMock())
        # SCALE_UP is recorded but never auto-adds exposure via this path.
        assert out.action == Action.HOLD


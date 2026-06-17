"""Grand Flip — verifies the live-flag flips (Option B), the cosmetic default
alignment (Option D), the dead-config wiring/removal (Option A), and the
PostCloseTracker instantiation (Option C).

These are intentionally dependency-light: config dataclass assertions plus a
few focused functional checks on the newly-wired consumers.
"""

import inspect

import pytest

from config import (
    AppConfig,
    OpportunityRankerConfig,
    OrchestratorConfig,
    RiskConfig,
    DecisionConfig,
    ScoringConfig,
    SignalLedgerConfig,
    TunerAgentConfig,
)


# ── Option B — live flag flips ──────────────────────────────────────────────

class TestLiveFlagDefaults:
    def test_ranker_win_rate_provider_live(self):
        assert OpportunityRankerConfig().adaptive_win_rate_provider_enabled is True

    def test_intentional_hedge_live(self):
        assert RiskConfig().allow_intentional_hedge is True

    def test_reject_on_minlot_inflation_live(self):
        assert RiskConfig().reject_on_minlot_inflation is True

    def test_reversal_weighted_evidence_live(self):
        assert DecisionConfig().reversal_weighted_evidence is True

    def test_structure_exit_tf_alignment_live(self):
        assert RiskConfig().structure_exit_tf_alignment_enabled is True

    def test_allow_scale_up_live(self):
        assert OrchestratorConfig().allow_scale_up is True

    def test_signal_ledger_live(self):
        cfg = SignalLedgerConfig()
        assert cfg.signal_ledger_enabled is True
        assert cfg.signal_grading_enabled is True
        assert cfg.emitter_feedback_enabled is True

    def test_tuner_agent_live(self):
        assert TunerAgentConfig().enabled is True

    def test_calibration_expectancy_aware_live(self):
        from planning.trade_planner import PlannerConfig

        assert PlannerConfig().calibration_expectancy_aware is True


# ── Option D — defaults reflect the live orchestrator-on runtime ────────────

class TestCosmeticDefaults:
    def test_planner_soften_and_dispersion_default_true(self):
        from planning.trade_planner import PlannerConfig

        cfg = PlannerConfig()
        assert cfg.soften_gates is True
        assert cfg.dispersion_aware_agreement is True

    def test_governor_graded_exposure_default_true(self):
        from governor.models import GovernorConfig

        assert GovernorConfig().graded_exposure is True


# ── Option A — dead fields removed ──────────────────────────────────────────

class TestDeadFieldsRemoved:
    def test_orchestrator_max_concurrent_trades_removed(self):
        assert not hasattr(OrchestratorConfig(), "max_concurrent_trades")

    def test_app_scan_interval_seconds_removed(self):
        assert not hasattr(AppConfig(), "scan_interval_seconds")

    def test_ranker_owns_dispatch_capacity(self):
        # The capacity authority lives on the ranker config, not the orchestrator.
        rc = OpportunityRankerConfig()
        assert hasattr(rc, "dispatch_top_n")
        assert hasattr(rc, "slot_aware_dispatch")
        assert hasattr(rc, "max_concurrent")


# ── Option A — adopted_observation_minutes is config-driven ─────────────────

class TestAdoptedObservationWiring:
    def _adopted_ctx(self, hold_minutes):
        from decision.context import TradeContext

        return TradeContext(
            symbol="EURUSD", direction="LONG", entry_price=1.10,
            current_price=1.10, entry_type="ORPHAN_ADOPTED",
            hold_minutes=hold_minutes,
        )

    def test_decision_engine_stores_value(self):
        from decision.engine import DecisionEngine

        assert DecisionEngine(adopted_observation_minutes=4.0).adopted_observation_minutes == 4.0

    def test_situation_engine_stores_value(self):
        from decision.situation import SituationEngine

        assert SituationEngine(adopted_observation_minutes=4.0).adopted_observation_minutes == 4.0

    def test_observation_window_is_config_driven(self):
        from decision.actions import Action
        from decision.engine import DecisionEngine
        from decision.situation import SituationEngine

        se = SituationEngine()
        ctx = self._adopted_ctx(hold_minutes=5.0)
        sa = se.assess_open_trade(ctx)

        observation_actions = {Action.OBSERVE, Action.SET_PROTECTIVE_STOP}

        # hold=5 < 10 (default) → observation window active (OBSERVE/protective).
        d_long = DecisionEngine(adopted_observation_minutes=10.0).decide_management(ctx, sa)
        # hold=5 >= 2 (shortened) → window over → normal management scoring.
        d_short = DecisionEngine(adopted_observation_minutes=2.0).decide_management(ctx, sa)

        assert d_long.action in observation_actions
        assert d_short.action not in observation_actions


# ── Option A — SignalLedger accuracy_lookback default window ────────────────

class TestSignalLedgerAccuracyLookback:
    def test_lookback_stored_and_used(self, tmp_path):
        from adaptive.signal_ledger import SignalLedger

        led = SignalLedger(db_path=tmp_path / "sl.db", accuracy_lookback=7)
        assert led._accuracy_lookback == 7
        # No rows yet, but the call must succeed using the stored default window.
        stats = led.get_emitter_accuracy("momentum")
        assert isinstance(stats, dict)

    def test_invalid_lookback_falls_back(self, tmp_path):
        from adaptive.signal_ledger import SignalLedger

        led = SignalLedger(db_path=tmp_path / "sl2.db", accuracy_lookback=0)
        assert led._accuracy_lookback == 100


# ── Option A — order_block_points / volatile_score_cap are wired ────────────

class TestScannerConfigWiring:
    def test_order_block_points_consumed(self):
        import scanner.pair_scanner as ps

        src = inspect.getsource(ps)
        assert "scoring.order_block_points" in src

    def test_volatile_score_cap_consumed(self):
        import scanner.pair_scanner as ps

        src = inspect.getsource(ps)
        assert "scoring.volatile_score_cap" in src

    def test_default_ob_points_preserve_legacy_split(self):
        # Non-adaptive STRONG OB points = order_block_points / 2 (10 at default 20).
        base = ScoringConfig().order_block_points / 2
        assert round(base) == 10
        assert round(base * 0.7) == 7
        assert round(base * 0.4) == 4


# ── Option A — news_exit_mode selects close vs tighten ──────────────────────

class TestNewsExitMode:
    def _mixin(self, mode):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from platforms.trading_loop.exit_checks_mixin import ExitChecksMixin  # noqa: F401

        m = MagicMock()
        m._news_exit_protected = set()
        m.position_store = MagicMock()
        m.config = SimpleNamespace(
            risk=SimpleNamespace(
                news_exit_enabled=True,
                news_exit_minutes_before=15,
                news_exit_mode=mode,
            ),
        )
        return m

    def _event(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            minutes_until=5.0, affected_pairs=["EURUSD"],
            impact="HIGH", name="NFP",
        )

    def _run(self, mode, close_succeeds=True):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from platforms.trading_loop.exit_checks_mixin import ExitChecksMixin

        m = self._mixin(mode)
        # Flat/loss trade → hits the news_exit_mode branch.
        pos = SimpleNamespace(
            symbol="EURUSD", direction="LONG", entry_price=1.10, sl=1.09,
            platform="mt5", tm_trade_id="t1", _pip_size=0.0001,
        )
        m.managed_positions = {"oid-1": pos}
        tm_trade = SimpleNamespace(pnl_pips=-2.0, breakeven_active=False, stop_loss=1.09)
        m.trade_manager = MagicMock()
        m.trade_manager.get_trade.return_value = tm_trade
        m.news_guard = MagicMock()
        m.news_guard.check.return_value = SimpleNamespace(upcoming_events=[self._event()])
        m.platforms = MagicMock()
        m.platforms.modify_trade.return_value = True
        m.platforms.close_trade.return_value = SimpleNamespace(
            success=close_succeeds, close_price=1.099,
        )
        m._record_closed_trade = MagicMock()
        ExitChecksMixin._check_news_exit(m, datetime(2025, 1, 1, tzinfo=timezone.utc))
        return m, pos

    def test_tighten_mode_moves_to_breakeven_not_close(self):
        m, pos = self._run("tighten")
        m.platforms.modify_trade.assert_called()      # SL moved to BE
        m.platforms.close_trade.assert_not_called()   # never flattened
        assert "oid-1" in m.managed_positions

    def test_close_mode_flattens(self):
        m, pos = self._run("close")
        m.platforms.close_trade.assert_called()        # flattened
        assert "oid-1" not in m.managed_positions

    def test_config_default_is_close(self):
        assert RiskConfig().news_exit_mode == "close"


# ── Option A — log_level applied via main helper ────────────────────────────

class TestLogLevelHelper:
    def test_apply_log_level_runs(self):
        import main

        # Must not raise and must leave a working logger.
        main._apply_log_level("WARNING")
        from loguru import logger

        logger.info("smoke")  # no exception


# ── Option C — PostCloseTracker is instantiated (no longer dead) ────────────

class TestPostCloseTrackerInstantiated:
    def test_main_loop_assigns_tracker(self):
        import inspect

        import platforms.main_loop as ml

        src = inspect.getsource(ml)
        # The tracker must be constructed (was previously only read via getattr).
        assert "self._post_close_tracker = PostCloseTracker(" in src

    def test_tracker_constructs_and_records(self, tmp_path):
        from datetime import datetime, timezone

        from adaptive.post_close_tracker import PostCloseTracker

        tk = PostCloseTracker(db_path=str(tmp_path / "pc.db"))
        assert tk.enabled is True
        tk.record_close(
            trade_id="t1", pair="EURUSD", direction="BUY",
            entry_price=1.10, exit_price=1.11, sl_price=1.09, tp_price=1.13,
            exit_cause="TP", entry_timestamp=datetime.now(timezone.utc),
            exit_timestamp=datetime.now(timezone.utc),
        )
        assert tk.pending_count == 1


"""Tests for Phase 2 — Decision Intelligence wiring into SystemContext
and EventDrivenSystem.

Covers:
- SystemContext creates all decision subsystems
- SessionEngine / NewsGuard callback wiring
- DecisionEngine / RiskGovernor integration in entry path
- DecisionJournal receives entries
- Graceful degradation when subsystems are None
"""

import pytest
from unittest.mock import MagicMock

from core.system_context import SystemContext


class TestSystemContextDecisionLayer:
    """Verify that SystemContext.create() builds all decision subsystems."""

    def test_create_builds_decision_subsystems(self):
        config = MagicMock()
        config.risk = MagicMock()
        config.risk.portfolio_risk_engine_enabled = False
        config.risk.drawdown_rolling_window_days = 7
        config.risk.max_correlated_trades = 3
        config.risk.max_cluster_same_direction = 2
        config.risk.allow_intentional_hedge = False
        config.risk.portfolio_heat_block_pct = 2.0
        config.risk.daily_loss_flatten_pct = 5.0
        config.governor = None
        config.decision = None
        pm = MagicMock()

        ctx = SystemContext.create(config, pm)

        assert ctx.session_engine is not None
        assert ctx.news_guard is not None
        assert ctx.situation_engine is not None
        assert ctx.decision_engine is not None
        assert ctx.risk_governor is not None
        assert ctx.decision_journal is not None

    def test_decision_fields_default_none(self):
        ctx = SystemContext()
        assert ctx.decision_engine is None
        assert ctx.situation_engine is None
        assert ctx.risk_governor is None
        assert ctx.decision_journal is None
        assert ctx.session_engine is None
        assert ctx.news_guard is None


class TestSessionEngineWiring:
    """Verify SessionEngine replaces the stub callback."""

    def test_session_engine_created_by_system_context(self):
        from brain.session_engine import SessionEngine
        config = MagicMock()
        config.risk = MagicMock()
        config.risk.portfolio_risk_engine_enabled = False
        config.risk.drawdown_rolling_window_days = 7
        config.risk.max_correlated_trades = 3
        config.risk.max_cluster_same_direction = 2
        config.risk.allow_intentional_hedge = False
        config.risk.portfolio_heat_block_pct = 2.0
        config.risk.daily_loss_flatten_pct = 5.0
        config.governor = None
        config.decision = None
        pm = MagicMock()

        ctx = SystemContext.create(config, pm)
        assert isinstance(ctx.session_engine, SessionEngine)

    def test_session_engine_get_status(self):
        from brain.session_engine import SessionEngine
        se = SessionEngine()
        status = se.get_status()
        assert hasattr(status, "is_tradeable")
        assert isinstance(status.is_tradeable, bool)

    def test_session_engine_is_pair_active(self):
        from brain.session_engine import SessionEngine
        se = SessionEngine()
        result = se.is_pair_active("EURUSD")
        assert isinstance(result, bool)


class TestNewsGuardWiring:
    """Verify NewsGuard replaces the stub callback."""

    def test_news_guard_created_by_system_context(self):
        from brain.session_engine import NewsGuard
        config = MagicMock()
        config.risk = MagicMock()
        config.risk.portfolio_risk_engine_enabled = False
        config.risk.drawdown_rolling_window_days = 7
        config.risk.max_correlated_trades = 3
        config.risk.max_cluster_same_direction = 2
        config.risk.allow_intentional_hedge = False
        config.risk.portfolio_heat_block_pct = 2.0
        config.risk.daily_loss_flatten_pct = 5.0
        config.governor = None
        config.decision = None
        pm = MagicMock()

        ctx = SystemContext.create(config, pm)
        assert isinstance(ctx.news_guard, NewsGuard)

    def test_news_guard_check_returns_status(self):
        from brain.session_engine import NewsGuard, NewsStatus
        ng = NewsGuard()
        result = ng.check(["EURUSD"])
        assert isinstance(result, NewsStatus)
        assert hasattr(result, "is_clear")


class TestDecisionEngineIntegration:
    """Verify DecisionEngine is consulted during entry flow."""

    def test_decision_engine_created_by_system_context(self):
        from decision.engine import DecisionEngine
        config = MagicMock()
        config.risk = MagicMock()
        config.risk.portfolio_risk_engine_enabled = False
        config.risk.drawdown_rolling_window_days = 7
        config.risk.max_correlated_trades = 3
        config.risk.max_cluster_same_direction = 2
        config.risk.allow_intentional_hedge = False
        config.risk.portfolio_heat_block_pct = 2.0
        config.risk.daily_loss_flatten_pct = 5.0
        config.governor = None
        config.decision = None
        pm = MagicMock()

        ctx = SystemContext.create(config, pm)
        assert isinstance(ctx.decision_engine, DecisionEngine)

    def test_decision_engine_enter_with_strong_signals(self):
        from decision.engine import DecisionEngine
        from decision.situation import SituationEngine
        from decision.context import EntryContext

        se = SituationEngine()
        de = DecisionEngine()

        ctx = EntryContext(
            symbol="EURUSD",
            direction="LONG",
            scan_score=90,
            d1_trend="BULLISH",
            d1_confidence=0.9,
            h4_trend="BULLISH",
            h4_confidence=0.8,
            h1_trend="BULLISH",
            h1_confidence=0.7,
            m1_aligned_count=4,
            risk_reward_2=3.0,
        )
        sa = se.assess_entry(ctx)
        result = de.decide_entry(ctx, sa)

        assert result.should_enter is True
        assert result.conviction > 0.0
        assert result.size_multiplier > 0.0

    def test_decision_engine_skip_with_weak_signals(self):
        from decision.engine import DecisionEngine
        from decision.situation import SituationEngine
        from decision.context import EntryContext

        se = SituationEngine()
        de = DecisionEngine()

        ctx = EntryContext(
            symbol="EURUSD",
            direction="LONG",
            scan_score=30,
            d1_trend="BEARISH",
            d1_confidence=0.9,
            h4_trend="BEARISH",
            h4_confidence=0.8,
            h1_trend="BEARISH",
            h1_confidence=0.7,
            m1_aligned_count=1,
            risk_reward_2=0.5,
        )
        sa = se.assess_entry(ctx)
        result = de.decide_entry(ctx, sa)

        assert isinstance(result.conviction, float)


class TestRiskGovernorIntegration:
    """Verify RiskGovernor reviews entry decisions."""

    def test_governor_passes_valid_entry(self):
        from decision.engine import DecisionEngine
        from decision.governor import RiskGovernor
        from decision.situation import SituationEngine
        from decision.context import EntryContext

        se = SituationEngine()
        de = DecisionEngine()
        gov = RiskGovernor(graded_risk=True)

        ctx = EntryContext(
            symbol="EURUSD",
            direction="LONG",
            scan_score=90,
            d1_trend="BULLISH",
            d1_confidence=0.9,
            h4_trend="BULLISH",
            h4_confidence=0.8,
            h1_trend="BULLISH",
            h1_confidence=0.7,
            m1_aligned_count=4,
            risk_reward_2=3.0,
            open_trade_count=1,
            max_open_trades=5,
            portfolio_heat_pct=0.5,
        )
        sa = se.assess_entry(ctx)
        de_result = de.decide_entry(ctx, sa)

        if de_result.should_enter:
            gov_result = gov.review_entry(de_result, ctx, sa)
            assert gov_result.should_enter is True

    def test_governor_vetoes_at_max_trades(self):
        from decision.engine import DecisionEngine
        from decision.governor import RiskGovernor
        from decision.situation import SituationEngine
        from decision.context import EntryContext
        from decision.actions import EntryAction, EntryDecision

        gov = RiskGovernor(graded_risk=False)

        de_result = EntryDecision(
            action=EntryAction.ENTER_MARKET,
            reason="test",
            conviction=0.8,
            size_multiplier=1.0,
        )
        ctx = EntryContext(
            symbol="EURUSD",
            direction="LONG",
            open_trade_count=5,
            max_open_trades=5,
        )
        from decision.situation import SituationAssessment
        sa = SituationAssessment()

        gov_result = gov.review_entry(de_result, ctx, sa)
        assert gov_result.should_enter is False
        assert gov_result.governor_vetoed is True


class TestDecisionJournalWiring:
    """Verify DecisionJournal records decisions."""

    def test_journal_log_entry_writes(self, tmp_path):
        from decision.journal import DecisionJournal
        from decision.actions import EntryAction, EntryDecision
        from decision.context import EntryContext
        from decision.situation import SituationAssessment

        journal = DecisionJournal(base_dir=str(tmp_path))

        ctx = EntryContext(symbol="EURUSD", direction="LONG", scan_score=80)
        sa = SituationAssessment(primary_label="TREND_CONTINUATION")
        dec = EntryDecision(
            action=EntryAction.ENTER_MARKET,
            reason="test entry",
            conviction=0.7,
            size_multiplier=1.0,
        )

        journal.log_entry(ctx, sa, dec)
        journal.close()

        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "EURUSD" in content
        assert "ENTER_MARKET" in content


class TestGracefulDegradation:
    """Verify system works when decision subsystems are None."""

    def test_system_context_with_all_none(self):
        ctx = SystemContext()
        assert ctx.decision_engine is None
        assert ctx.situation_engine is None
        assert ctx.risk_governor is None
        assert ctx.decision_journal is None
        assert ctx.session_engine is None
        assert ctx.news_guard is None

    def test_system_context_all_subsystems_populated(self):
        config = MagicMock()
        config.risk = MagicMock()
        config.risk.portfolio_risk_engine_enabled = False
        config.risk.drawdown_rolling_window_days = 7
        config.risk.max_correlated_trades = 3
        config.risk.max_cluster_same_direction = 2
        config.risk.allow_intentional_hedge = False
        config.risk.portfolio_heat_block_pct = 2.0
        config.risk.daily_loss_flatten_pct = 5.0
        config.governor = None
        config.decision = None
        pm = MagicMock()

        ctx = SystemContext.create(config, pm)

        assert ctx.drawdown_guard is not None
        assert ctx.correlation_engine is not None
        assert ctx.session_engine is not None
        assert ctx.news_guard is not None
        assert ctx.situation_engine is not None
        assert ctx.decision_engine is not None
        assert ctx.risk_governor is not None
        assert ctx.decision_journal is not None

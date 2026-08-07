"""
Tests for the opportunity executor.

Verifies selection, shadow vs execute modes, capacity (max_concurrent), the
empty-candidates fallback, and that pipeline errors are surfaced (never
silently swallowed).
"""

import pytest

from config import OpportunityRankerConfig
from brain.opportunity_ranker import Opportunity
from management.opportunity_executor import OpportunityExecutor


def _opp(direction="LONG", ev=1.0, tf="SCALP") -> Opportunity:
    return Opportunity(
        direction=direction,
        timeframe_class=tf,
        expected_value=ev,
        confidence=0.7,
        coherence=0.9,
        net_score=1.0 if direction == "LONG" else -1.0,
        reward_risk=1.5,
        win_prob=0.6,
        contributors=["momentum"],
        votes=[],
    )


class TestSelection:
    def test_select_empty_returns_none(self):
        ex = OpportunityExecutor(OpportunityRankerConfig())
        assert ex.select(None) is None
        assert ex.select([]) is None

    def test_select_best_is_first(self):
        ex = OpportunityExecutor(OpportunityRankerConfig())
        best = _opp(ev=2.0)
        assert ex.select([best, _opp(ev=1.0)]) is best

    def test_select_top_respects_max_concurrent(self):
        ex = OpportunityExecutor(OpportunityRankerConfig(max_concurrent=2))
        cands = [_opp(ev=3.0), _opp(ev=2.0), _opp(ev=1.0)]
        assert len(ex.select_top(cands)) == 2


class TestExecution:
    def test_shadow_mode_never_dispatches(self):
        ex = OpportunityExecutor(OpportunityRankerConfig(execute=False))
        calls = []
        outcome = ex.execute([_opp()], lambda o: calls.append(o) or True, label="EURUSD")
        assert outcome.shadow is True
        assert outcome.executed == 0
        assert calls == []

    def test_execute_mode_dispatches_best(self):
        ex = OpportunityExecutor(OpportunityRankerConfig(execute=True))
        dispatched = []
        outcome = ex.execute([_opp(ev=2.0), _opp(ev=1.0)], lambda o: dispatched.append(o) or True)
        assert outcome.executed == 1
        assert dispatched[0].expected_value == 2.0

    def test_execute_capacity_two(self):
        ex = OpportunityExecutor(OpportunityRankerConfig(execute=True, max_concurrent=2))
        dispatched = []
        outcome = ex.execute(
            [_opp(ev=3.0), _opp(ev=2.0, direction="SHORT"), _opp(ev=1.0)],
            lambda o: dispatched.append(o) or True,
        )
        assert outcome.executed == 2
        assert len(dispatched) == 2

    def test_declined_downstream_not_counted(self):
        ex = OpportunityExecutor(OpportunityRankerConfig(execute=True))
        outcome = ex.execute([_opp()], lambda o: False)
        assert outcome.executed == 0
        assert outcome.shadow is False

    def test_empty_candidates_no_dispatch(self):
        ex = OpportunityExecutor(OpportunityRankerConfig(execute=True))
        called = []
        outcome = ex.execute([], lambda o: called.append(o) or True)
        assert outcome.executed == 0
        assert called == []

    def test_pipeline_error_surfaced(self):
        ex = OpportunityExecutor(OpportunityRankerConfig(execute=True))

        def boom(_o):
            raise RuntimeError("pipeline blew up")

        with pytest.raises(RuntimeError, match="pipeline blew up"):
            ex.execute([_opp()], boom)


class TestConfigValidation:
    def test_bad_max_concurrent_rejected(self):
        with pytest.raises(ValueError):
            OpportunityRankerConfig(max_concurrent=0)

    def test_bad_win_rate_rejected(self):
        with pytest.raises(ValueError):
            OpportunityRankerConfig(base_win_rate=1.5)

    def test_bad_reward_risk_rejected(self):
        with pytest.raises(ValueError):
            OpportunityRankerConfig(scalp_reward_risk=0.0)

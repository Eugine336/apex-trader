"""
Tests for the management-layer opportunity executor — no torch/pandas needed.
"""

from __future__ import annotations

from brain.directional_consensus import Vote
from brain.opportunity_ranker import rank_opportunities
from management.opportunity_executor import OpportunityExecutor


def _opps():
    votes = [
        Vote("structure", "LONG", 0.9, 1.0),
        Vote("currency_strength", "LONG", 0.85, 1.0),
        Vote("momentum", "SHORT", 0.9, 1.0),
        Vote("vwap", "SHORT", 0.85, 1.0),
        Vote("fvg", "SHORT", 0.8, 1.0),
    ]
    return rank_opportunities(votes)


class TestExecutor:
    def test_empty_returns_no_outcomes(self):
        ex = OpportunityExecutor(execute=True)
        assert ex.execute_opportunities([], lambda o: "x") == []

    def test_shadow_mode_does_not_dispatch(self):
        ex = OpportunityExecutor(max_concurrent=5, execute=False)
        calls = []
        outcomes = ex.execute_opportunities(_opps(), lambda o: calls.append(o))
        assert calls == []  # pipeline never invoked
        assert all(o.allocated and not o.dispatched for o in outcomes)

    def test_execute_dispatches_up_to_capacity(self):
        ex = OpportunityExecutor(max_concurrent=1, execute=True)
        calls = []
        outcomes = ex.execute_opportunities(
            _opps(), lambda o: calls.append(o.direction) or "ok"
        )
        dispatched = [o for o in outcomes if o.dispatched]
        assert len(dispatched) == 1
        assert len(calls) == 1
        # the rest are skipped for capacity
        assert any("capacity" in o.skipped_reason for o in outcomes)

    def test_dispatches_best_first(self):
        ex = OpportunityExecutor(max_concurrent=1, execute=True)
        opps = _opps()
        seen = []
        ex.execute_opportunities(opps, lambda o: seen.append(o))
        assert seen[0] is opps[0]  # highest-ranked dispatched first

    def test_pipeline_error_surfaced_not_swallowed(self):
        ex = OpportunityExecutor(max_concurrent=2, execute=True)

        def boom(_o):
            raise RuntimeError("broker down")

        outcomes = ex.execute_opportunities(_opps(), boom)
        errored = [o for o in outcomes if o.error is not None]
        assert errored
        assert isinstance(errored[0].error, RuntimeError)

    def test_from_config(self):
        from config import OpportunityRankerConfig
        cfg = OpportunityRankerConfig(execute=True, max_concurrent_opportunities=3)
        ex = OpportunityExecutor.from_config(cfg)
        assert ex.execute is True
        assert ex.max_concurrent == 3

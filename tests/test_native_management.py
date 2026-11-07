"""Tests for the deterministic, provider-independent campaign manager."""

import time

from cognition.brain import CognitiveBrain, PositionView
from cognition.contracts import DecisionType, Evidence, MarketState
from cognition.identity import IDENTITY_VERSION
from cognition.memory import CampaignMemoryStore
from cognition.native_management import NativePositionManager


def _position(*, campaign_id="campaign-1", profit_r=0.0):
    return type("Position", (), {
        "symbol": "EURUSD",
        "direction": "LONG",
        "campaign_id": campaign_id,
        "profit_r": profit_r,
    })()


def _state(*, markers=(), uncertainty=0.1, add_cost=False):
    state = MarketState(symbol="EURUSD")
    market_domains = ("structure", "momentum", "volatility")
    for index, domain in enumerate(market_domains):
        measurements = {}
        if index < len(markers):
            marker_state = markers[index].get("state", "intact")
            source_group = f"verified-test-source:{domain}"
            measurements = {
                "campaign_id": "campaign-1",
                "native_thesis_state": marker_state,
                "independence_group": source_group,
            }
            if markers[index].get("ev") is not None:
                measurements["expected_value_after_cost_r"] = markers[index]["ev"]
        state.add(Evidence(
            source_module=f"native.{domain}",
            domain=domain,
            symbol="EURUSD",
            observation=f"fresh {domain} evidence",
            confidence=0.9,
            uncertainty=uncertainty,
            measurements=measurements,
            source_timestamp_epoch=time.time() if measurements else None,
            source_timestamp_verified=bool(measurements),
            independence_group=(
                str(measurements.get("independence_group", "")) if measurements else ""
            ),
            relevance_horizon_seconds=900.0,
        ))
    if add_cost:
        state.add(Evidence(
            source_module="execution_cost.observer",
            domain="execution_quality",
            symbol="EURUSD",
            observation="fresh executable-cost estimate",
            confidence=0.9,
            uncertainty=uncertainty,
            measurements={"estimated_total_cost_r": 0.12},
            relevance_horizon_seconds=300.0,
        ))
    return state


def _context():
    return {
        "campaign_id": "campaign-1",
        "original_thesis": "The original structural opportunity remains viable while the break holds.",
        "invalidation_conditions": ["The structure supporting the break fails."],
    }


def test_two_independent_domains_confirmed_invalidation_can_exit():
    manager = NativePositionManager()
    state = _state(markers=({"state": "invalidated"}, {"state": "invalidated"}))
    result = manager.assess(
        _position(), state, campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.1, "market_domain_count": 3},
    )
    assert result.action == "exit"
    assert result.thesis_state == "invalidated"
    assert len(result.evidence_domains) == 2
    assert result.identity_version == IDENTITY_VERSION
    assert result.to_dict()["identity_version"] == IDENTITY_VERSION


def test_single_domain_invalidation_does_not_liquidate():
    manager = NativePositionManager()
    state = _state(markers=({"state": "invalidated"},))
    result = manager.assess(
        _position(), state, campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.1, "market_domain_count": 3},
    )
    assert result.action == "hold"
    assert result.thesis_state == "invalidation_unconfirmed"


def test_invalidation_requires_original_thesis_and_invalidation_conditions():
    manager = NativePositionManager()
    state = _state(markers=({"state": "invalidated"}, {"state": "invalidated"}))
    result = manager.assess(
        _position(), state, campaign_context={"campaign_id": "campaign-1"},
        consolidation={"aggregate_uncertainty": 0.1, "market_domain_count": 3},
    )
    assert result.action == "hold"
    assert "original thesis" in result.reason


def test_high_market_uncertainty_never_triggers_exit():
    manager = NativePositionManager()
    state = _state(
        markers=({"state": "invalidated"}, {"state": "invalidated"}),
        uncertainty=0.9,
    )
    result = manager.assess(
        _position(), state, campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.9, "market_domain_count": 3},
    )
    assert result.action == "hold"
    assert result.thesis_state == "invalidation_unconfirmed"


def test_deterioration_reduces_exposure_without_exiting_on_doubt():
    manager = NativePositionManager()
    state = _state(markers=({"state": "deteriorating"}, {"state": "weakening"}))
    result = manager.assess(
        _position(profit_r=0.7), state, campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.2, "market_domain_count": 3},
    )
    assert result.action == "scale_out"
    assert result.thesis_state == "deteriorating"


def test_deterioration_tightens_risk_when_position_is_not_profitable():
    manager = NativePositionManager()
    state = _state(markers=({"state": "deteriorating"}, {"state": "weakening"}))
    result = manager.assess(
        _position(profit_r=-0.2), state, campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.2, "market_domain_count": 3},
    )
    assert result.action == "tighten_risk"


def test_non_positive_net_ev_reduces_risk_but_does_not_auto_exit():
    manager = NativePositionManager()
    state = _state(
        markers=({"state": "intact", "ev": -0.1}, {"state": "intact", "ev": -0.2}),
        add_cost=True,
    )
    result = manager.assess(
        _position(), state, campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.2, "market_domain_count": 3},
    )
    assert result.action == "tighten_risk"
    assert result.expected_value_after_cost_r == -0.2


def test_campaign_linked_whole_book_risk_breach_only_derisks():
    manager = NativePositionManager()
    state = _state()
    state.add(Evidence(
        source_module="portfolio.risk_monitor",
        domain="structure",
        symbol="EURUSD",
        observation="campaign-specific whole-book risk breach",
        confidence=0.9,
        uncertainty=0.1,
        measurements={
            "campaign_id": "campaign-1",
            "whole_book_risk_state": "breach",
        },
        relevance_horizon_seconds=900.0,
    ))
    result = manager.assess(
        _position(), state, campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.8, "market_domain_count": 3},
    )
    assert result.action == "tighten_risk"


def test_changed_direction_or_absent_campaign_assessment_never_exits():
    manager = NativePositionManager()
    state = _state()
    result = manager.assess(
        _position(), state, campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.1, "market_domain_count": 3},
    )
    assert result.action == "hold"
    assert "no fresh campaign-linked thesis assessment" in result.reason


def test_repeating_same_assessment_is_idempotent():
    manager = NativePositionManager()
    state = _state(markers=({"state": "deteriorating"}, {"state": "weakening"}))
    args = {
        "position": _position(profit_r=0.7),
        "market_state": state,
        "campaign_context": _context(),
        "consolidation": {"aggregate_uncertainty": 0.2, "market_domain_count": 3},
    }
    first = manager.assess(**args).to_dict()
    second = manager.assess(**args).to_dict()
    assert first == second


def test_native_manager_fault_fails_safe_to_hold():
    class _BrokenMarketState:
        symbol = "EURUSD"

        def consolidation(self, now=None):
            raise RuntimeError("synthetic market-state failure")

        def fresh_evidence(self):
            return []

    manager = NativePositionManager()
    result = manager.assess(_position(), _BrokenMarketState(), campaign_context=_context())
    assert result.action == "hold"
    assert "fault" in result.reason



def test_brain_uses_native_management_when_no_provider_and_loads_original_thesis():
    store = CampaignMemoryStore(db_path=":memory:")
    store.record_open(
        symbol="EURUSD",
        direction="LONG",
        fingerprint={},
        campaign_id="campaign-1",
        spec={
            "thesis": "The original structural break remains valid.",
            "invalidation_conditions": ["The broken structure is reclaimed."],
            "opportunity_id": "opportunity-1",
            "decision_id": "decision-1",
            "identity_version": IDENTITY_VERSION,
        },
    )
    brain = CognitiveBrain(reasoner=None)
    brain.bind_campaign_memory(store)
    state = _state(markers=({"state": "invalidated"}, {"state": "invalidated"}))
    try:
        out = brain.manage(
            PositionView(
                symbol="EURUSD", direction="LONG", campaign_id="campaign-1",
            ),
            state,
        )
        assert out.decision.decision_type == DecisionType.EXIT
        baseline = out.decision.questions_answered["native_management_baseline"]
        assert baseline["action"] == "exit"
        assert baseline["campaign_id"] == "campaign-1"
        assert baseline["original_thesis"] == "The original structural break remains valid."
        assert out.decision.to_dict()["identity_version"] == IDENTITY_VERSION
    finally:
        store.close()


def test_brain_records_native_hold_when_no_provider_and_no_campaign_assessment():
    brain = CognitiveBrain(reasoner=None)
    state = _state()
    out = brain.manage(
        PositionView(symbol="EURUSD", direction="LONG", campaign_id="campaign-1"),
        state,
    )
    assert out.decision.decision_type == DecisionType.HOLD
    baseline = out.decision.questions_answered["native_management_baseline"]
    assert baseline["action"] == "hold"
    assert "no fresh campaign-linked thesis assessment" in baseline["reason"]
    assert out.decision.to_dict()["identity_version"] == IDENTITY_VERSION


def test_native_patterns_can_reduce_risk_without_campaign_tags():
    from types import SimpleNamespace

    manager = NativePositionManager()
    state = _state()
    analysis = SimpleNamespace(
        domain_coverage={"structure": 0.9, "momentum": 0.8, "volatility": 0.8},
        detected_patterns=["momentum_divergence", "multi_tf_conflict"],
        contradictions=["price direction and momentum diverge"],
        evidence_uncertainty=0.2,
    )
    result = manager.assess(
        _position(profit_r=0.6), state, campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.2, "market_domain_count": 3},
        analysis=analysis,
        hypotheses=[],
    )
    assert result.action == "scale_out"
    assert result.thesis_state == "native_evidence_deteriorating"
    assert "not exit" in result.reason


def test_counter_direction_alone_does_not_reduce_or_exit():
    from types import SimpleNamespace

    manager = NativePositionManager()
    state = _state()
    analysis = SimpleNamespace(
        domain_coverage={"structure": 0.9, "momentum": 0.8, "volatility": 0.8},
        detected_patterns=[],
        contradictions=[],
        evidence_uncertainty=0.2,
    )
    opposite_hypothesis = SimpleNamespace(
        direction_implication="SHORT",
        confidence=0.95,
        contradicting_patterns=[],
    )
    result = manager.assess(
        _position(), state, campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.2, "market_domain_count": 3},
        analysis=analysis,
        hypotheses=[opposite_hypothesis],
    )
    assert result.action == "hold"


def test_brain_ignores_campaign_memory_for_mismatched_position():
    store = CampaignMemoryStore(db_path=":memory:")
    store.record_open(
        symbol="EURUSD",
        direction="SHORT",
        fingerprint={},
        campaign_id="campaign-mismatch",
        spec={
            "thesis": "Thesis from a different held side",
            "invalidation_conditions": ["Different side condition"],
            "identity_version": IDENTITY_VERSION,
        },
    )
    brain = CognitiveBrain(reasoner=None)
    brain.bind_campaign_memory(store)
    state = _state(markers=({"state": "invalidated"}, {"state": "invalidated"}))
    try:
        out = brain.manage(
            PositionView(
                symbol="EURUSD", direction="LONG", campaign_id="campaign-mismatch",
            ),
            state,
        )
        assert out.decision.decision_type == DecisionType.HOLD
        baseline = out.decision.questions_answered["native_management_baseline"]
        assert baseline["action"] == "hold"
        assert baseline["original_thesis"] == ""
        assert "no fresh campaign-linked thesis assessment" in baseline["reason"]
    finally:
        store.close()


def test_invalidation_requires_two_verified_independent_source_lineages():
    manager = NativePositionManager()
    state = MarketState(symbol="EURUSD")
    for domain, group in (("structure", "ohlc:M15:source-a"),
                          ("momentum", "ticks:source-b")):
        stamp = time.time()
        state.add(Evidence(
            source_module=f"native.{domain}",
            domain=domain,
            symbol="EURUSD",
            observation=f"fresh {domain} campaign invalidation",
            confidence=0.9,
            uncertainty=0.1,
            measurements={
                "campaign_id": "campaign-1",
                "native_thesis_state": "invalidated",
                "independence_group": group,
            },
            source_timestamp_epoch=stamp,
            source_timestamp_verified=True,
            independence_group=group,
            relevance_horizon_seconds=900.0,
        ))
    state.add(Evidence(
        source_module="portfolio.risk_monitor",
        domain="other",
        symbol="EURUSD",
        observation="risk dashboard repeats an invalidation status",
        confidence=0.99,
        uncertainty=0.1,
        measurements={
            "campaign_id": "campaign-1",
            "native_thesis_state": "invalidated",
        },
        relevance_horizon_seconds=900.0,
    ))
    result = manager.assess(
        _position(), state, campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.1},
    )
    assert result.action == "exit"
    assert result.thesis_state == "invalidated"


def test_unknown_or_shared_source_lineage_cannot_manufacture_invalidation_confirmation():
    manager = NativePositionManager()
    state = MarketState(symbol="EURUSD")
    for domain in ("structure", "momentum"):
        state.add(Evidence(
            source_module=f"native.{domain}",
            domain=domain,
            symbol="EURUSD",
            observation=f"fresh {domain} evidence",
            confidence=0.9,
            uncertainty=0.1,
            measurements={
                "campaign_id": "campaign-1",
                "native_thesis_state": "invalidated",
                "independence_group": "same-source:ohlc",
            },
            source_timestamp_epoch=time.time(),
            source_timestamp_verified=True,
            independence_group="same-source:ohlc",
            relevance_horizon_seconds=900.0,
        ))
    result = manager.assess(
        _position(), state, campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.1},
    )
    assert result.action == "hold"
    assert result.thesis_state == "invalidation_unconfirmed"
    assert "independent invalidation lineages" in result.reason


def test_process_local_thesis_is_not_reused_across_campaign_ids():
    from types import SimpleNamespace

    brain = CognitiveBrain(reasoner=None)
    brain._last["EURUSD"] = SimpleNamespace(
        campaign=SimpleNamespace(campaign_id="older-campaign"),
        decision=SimpleNamespace(
            thesis="thesis from a closed/older position",
            invalidation_conditions=["old invalidation condition"],
            decision_type=DecisionType.OPEN_CAMPAIGN,
        ),
    )
    position = PositionView(
        symbol="EURUSD", direction="LONG", campaign_id="current-campaign",
    )
    state = _state()
    payload = brain._management_evidence_payload(
        "EURUSD", state, state.consolidation(), position,
    )
    campaign = payload["campaign"]
    assert "original_thesis" not in campaign
    assert "invalidation_conditions" not in campaign


def test_expected_holding_horizon_triggers_reassessment_not_automatic_exit():
    manager = NativePositionManager()
    position = _position(profit_r=0.1)
    position.hold_seconds = 7_200.0
    context = _context()
    context["expected_holding_horizon_seconds"] = 3_600.0

    result = manager.assess(
        position,
        _state(),
        campaign_context=context,
        consolidation={"aggregate_uncertainty": 0.1, "market_domain_count": 3},
    )

    assert result.action == "hold"
    assert result.thesis_state == "time_decay_review"
    assert result.management_plan["elapsed_ratio"] == 2.0



def test_campaign_geometry_completed_close_invalidation_is_an_exit_verdict():
    import time

    now = time.time()
    state = _state()
    state.add(Evidence(
        source_module="market_lenses.h1.structure",
        domain="structure",
        symbol="EURUSD",
        observation="completed H1 close breached the campaign's original concept invalidation",
        confidence=0.9,
        uncertainty=0.1,
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "provisional": False,
            "timeframe": "H1",
            "source_bar_epoch": now - 10.0,
            "current_close": 97.9,
            "prior_range_low": 97.5,
        },
        timestamp_epoch=now - 10.0,
        relevance_horizon_seconds=900.0,
    ))
    context = _context()
    context["concept_geometry"] = {
        "concept": "market_structure",
        "concept_timeframe": "H1",
        "entry_price": 100.0,
        "stop_price": 98.0,
        "target_price": 106.0,
        "milestones": [102.0, 104.0],
        "evidence_timestamp_epoch": now - 60.0,
        "symbol": "EURUSD",
        "direction": "LONG",
    }

    result = NativePositionManager().assess(
        _position(), state, campaign_context=context, now=now,
    )

    assert result.action == "exit"
    assert result.thesis_state == "concept_invalidation_confirmed"
    assert result.management_plan["kind"] == "invalidation_exit"
    assert result.management_plan["action_id"].startswith("campaign-1:concept-invalidation:")


def test_low_confidence_counter_hypothesis_with_fresh_contradiction_can_derisk():
    from types import SimpleNamespace

    manager = NativePositionManager()
    state = _state()
    analysis = SimpleNamespace(
        domain_coverage={},
        detected_patterns=["momentum_divergence"],
        contradictions=["fresh price/momentum divergence"],
        evidence_uncertainty=0.9,
    )
    counter = SimpleNamespace(
        direction_implication="SHORT",
        confidence=0.10,
        contradicting_patterns=["momentum_divergence"],
    )
    result = manager.assess(
        _position(profit_r=0.5),
        state,
        campaign_context=_context(),
        consolidation={"aggregate_uncertainty": 0.9},
        analysis=analysis,
        hypotheses=[counter],
    )
    assert result.action == "scale_out"
    assert result.thesis_state == "native_evidence_deteriorating"
    assert "not exit" in result.reason

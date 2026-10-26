"""Tests for the deterministic, provider-independent campaign manager."""

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
            measurements = {
                "campaign_id": "campaign-1",
                "native_thesis_state": marker_state,
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

"""Tests for the deterministic, provider-independent campaign manager."""

from cognition.native_management import NativePositionManager
from cognition.contracts import Evidence, MarketState
from cognition.identity import IDENTITY_VERSION


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
    manager = NativePositionManager()
    result = manager.assess(None, None)
    assert result.action == "hold"
    assert "fault" in result.reason or "campaign id missing" in result.reason

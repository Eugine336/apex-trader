"""Identity version and provenance must survive every cognitive boundary."""

from cognition.contracts import (
    CampaignSpecification,
    DecisionPackage,
    Evidence,
    EvidenceDomain,
    Hypothesis,
    Opportunity,
)
from cognition.identity import IDENTITY_VERSION
from cognition.provenance import attach_provenance, current_provenance
from execution.action_executor import ActionExecutor
from execution.intents import Intent


def test_cognitive_contract_serializers_carry_identity_version():
    records = (
        Evidence(source_module="test", domain=EvidenceDomain.STRUCTURE),
        Hypothesis(statement="test hypothesis"),
        Opportunity(opportunity_id="op-1"),
        DecisionPackage(symbol="TEST"),
        CampaignSpecification(symbol="TEST"),
    )

    for record in records:
        assert record.to_dict()["identity_version"] == IDENTITY_VERSION


def test_provenance_helper_preserves_links_without_mutating_input():
    source = {"decision_id": "d-1", "campaign_id": "c-1", "payload": {"k": "v"}}
    result = attach_provenance(source, opportunity_id="o-1", event_id="e-1")

    assert source == {"decision_id": "d-1", "campaign_id": "c-1", "payload": {"k": "v"}}
    assert result["identity_version"] == IDENTITY_VERSION
    assert result["decision_id"] == "d-1"
    assert result["campaign_id"] == "c-1"
    assert result["opportunity_id"] == "o-1"
    assert result["event_id"] == "e-1"
    assert result["parent_event_id"] == ""


def test_current_provenance_does_not_invent_missing_event_links():
    provenance = current_provenance()
    assert provenance["identity_version"] == IDENTITY_VERSION
    assert provenance["decision_id"] == ""
    assert provenance["opportunity_id"] == ""
    assert provenance["campaign_id"] == ""
    assert provenance["event_id"] == ""


def test_missing_provenance_exception_is_limited_to_de_risking_intents():
    close = Intent.close(
        symbol="TEST", ticket="7", source="legacy", reason="protect position",
        identity_version="",
    )
    partial = Intent.partial_close(
        symbol="TEST", ticket="7", fraction=0.25, source="legacy",
        reason="reduce exposure", identity_version="",
    )
    tight_long_stop = Intent.modify_sl(
        symbol="TEST", ticket="7", new_sl=1.15, source="legacy",
        reason="tighten risk", identity_version="",
    )
    loosen_long_stop = Intent.modify_sl(
        symbol="TEST", ticket="7", new_sl=0.85, source="legacy",
        reason="loosen risk", identity_version="",
    )
    change_target = Intent.modify_tp(
        symbol="TEST", ticket="7", new_tp=1.3, source="legacy",
        reason="change target", identity_version="",
    )
    positions = {"7": {"symbol": "TEST", "direction": "LONG", "sl": 1.0}}

    assert ActionExecutor._is_risk_reducing_without_provenance(close, positions)
    assert ActionExecutor._is_risk_reducing_without_provenance(partial, positions)
    assert ActionExecutor._is_risk_reducing_without_provenance(tight_long_stop, positions)
    assert not ActionExecutor._is_risk_reducing_without_provenance(loosen_long_stop, positions)
    assert not ActionExecutor._is_risk_reducing_without_provenance(change_target, positions)

"""Offline tests for the capability registry + Action Planner (Part IX, Art 3/11)."""

from action.capabilities import (
    CAP_GITHUB_CREATE_ISSUE,
    CAP_OPERATOR_NOTIFY,
    Capability,
    CapabilityCategory,
    ProviderBinding,
    default_registry,
)
from action.orchestrator import ActionOrchestrator, GovernancePolicy, RiskTier
from action.planner import ActionPlanner, ObjectiveRequest


# ── Capability registry + provider selection ─────────────────────────────────

def test_default_registry_has_curated_capabilities():
    reg = default_registry()
    for name in (CAP_OPERATOR_NOTIFY, CAP_GITHUB_CREATE_ISSUE,
                 "docs.update", "report.publish", "research.record",
                 "knowledge.retrieve", "noop"):
        assert reg.get(name) is not None


def test_unknown_capability_is_none():
    assert default_registry().get("delete.everything") is None


def test_resolve_provider_prefers_available_candidate():
    reg = default_registry()
    notify = reg.get(CAP_OPERATOR_NOTIFY)
    # Only telegram available → telegram chosen even though slack is first.
    b = reg.resolve_provider(notify, available={"telegram"})
    assert b.provider == "telegram" and b.action == "TELEGRAM_SEND_MESSAGE"


def test_resolve_provider_honours_preference():
    reg = default_registry()
    notify = reg.get(CAP_OPERATOR_NOTIFY)
    b = reg.resolve_provider(notify, preferred="telegram", available={"slack", "telegram"})
    assert b.provider == "telegram"


def test_resolve_provider_unconstrained_takes_first():
    reg = default_registry()
    notify = reg.get(CAP_OPERATOR_NOTIFY)
    assert reg.resolve_provider(notify).provider == "slack"


def test_resolve_provider_none_when_no_candidate_available():
    reg = default_registry()
    gh = reg.get(CAP_GITHUB_CREATE_ISSUE)
    assert reg.resolve_provider(gh, available={"slack"}) is None


def test_missing_params_detected():
    reg = default_registry()
    gh = reg.get(CAP_GITHUB_CREATE_ISSUE)
    assert set(gh.missing_params({"title": "x"})) == {"body"}
    assert gh.missing_params({"title": "x", "body": "y"}) == []


# ── Action Planner ────────────────────────────────────────────────────────────

def _orchestrator(**policy_kw):
    from action.composio import MockActionAdapter
    params = dict(enabled=True, auto_max_risk=RiskTier.MEDIUM)
    params.update(policy_kw)
    return ActionOrchestrator(GovernancePolicy(**params), MockActionAdapter())


def test_planner_disabled_is_inert():
    planner = ActionPlanner(_orchestrator(), enabled=False)
    rec = planner.submit(ObjectiveRequest(intent=CAP_OPERATOR_NOTIFY,
                                          params={"message": "hi"}, source="ai_brain"))
    assert rec is None
    assert planner.get_status()["submitted"] == 0


def test_planner_builds_provider_bound_objective():
    planner = ActionPlanner(_orchestrator(), enabled=True)
    obj = planner.build_objective(ObjectiveRequest(
        intent=CAP_OPERATOR_NOTIFY, objective="notify op", params={"message": "hi"},
        source="ai_brain", confidence=0.9))
    assert obj is not None
    # capability becomes the concrete Composio action for the chosen provider
    assert obj.capability == "SLACK_SEND_MESSAGE"
    assert obj.params["_capability"] == CAP_OPERATOR_NOTIFY
    assert obj.params["_provider"] == "slack"
    assert obj.risk_tier == RiskTier.NEGLIGIBLE


def test_planner_rejects_unknown_capability():
    planner = ActionPlanner(_orchestrator(), enabled=True)
    assert planner.submit(ObjectiveRequest(intent="rm.rf", source="ai_brain")) is None
    assert planner.get_status()["rejected"] == 1


def test_planner_rejects_missing_params():
    planner = ActionPlanner(_orchestrator(), enabled=True)
    # github.create_issue requires title+body
    assert planner.build_objective(ObjectiveRequest(
        intent=CAP_GITHUB_CREATE_ISSUE, params={"title": "x"}, source="ai_brain")) is None
    assert planner.get_status()["rejected"] == 1


def test_planner_submits_low_risk_and_executes():
    orch = _orchestrator()
    planner = ActionPlanner(orch, enabled=True)
    rec = planner.submit(ObjectiveRequest(
        intent=CAP_OPERATOR_NOTIFY, params={"message": "hi"}, source="ai_brain",
        confidence=0.9))
    assert rec is not None
    assert rec.status.value == "executed"      # mock adapter succeeds
    assert planner.get_status()["submitted"] == 1


def test_planner_medium_risk_requires_approval_without_confidence():
    # github.create_issue is MEDIUM; with a LOW auto-ceiling and low confidence
    # the orchestrator requires approval.
    orch = _orchestrator(auto_max_risk=RiskTier.LOW, medium_confidence_threshold=0.9)
    planner = ActionPlanner(orch, enabled=True)
    rec = planner.submit(ObjectiveRequest(
        intent=CAP_GITHUB_CREATE_ISSUE, params={"title": "t", "body": "b"},
        source="ai_brain", confidence=0.3))
    assert rec is not None
    assert rec.status.value == "pending_approval"


def test_planner_availability_filter_rejects_when_no_provider():
    orch = _orchestrator()
    planner = ActionPlanner(orch, enabled=True, available_providers=["slack"])
    # github only has a github provider → filtered out → rejected
    rec = planner.submit(ObjectiveRequest(
        intent=CAP_GITHUB_CREATE_ISSUE, params={"title": "t", "body": "b"},
        source="ai_brain", confidence=0.9))
    assert rec is None
    assert planner.get_status()["rejected"] == 1

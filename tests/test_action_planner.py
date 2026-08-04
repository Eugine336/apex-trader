"""Offline tests for the capability registry + Action Planner (Part IX, Art 3/11)."""

from action.capabilities import (
    CAP_CREATE_ENGINEERING_TASK,
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
    for name in (CAP_OPERATOR_NOTIFY, CAP_CREATE_ENGINEERING_TASK,
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
    gh = reg.get(CAP_CREATE_ENGINEERING_TASK)
    assert reg.resolve_provider(gh, available={"slack"}) is None


def test_missing_params_detected():
    reg = default_registry()
    gh = reg.get(CAP_CREATE_ENGINEERING_TASK)
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
    # engineering.create_task requires title+body
    assert planner.build_objective(ObjectiveRequest(
        intent=CAP_CREATE_ENGINEERING_TASK, params={"title": "x"}, source="ai_brain")) is None
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
    # engineering.create_task is MEDIUM; with a LOW auto-ceiling and low confidence
    # the orchestrator requires approval.
    orch = _orchestrator(auto_max_risk=RiskTier.LOW, medium_confidence_threshold=0.9)
    planner = ActionPlanner(orch, enabled=True)
    rec = planner.submit(ObjectiveRequest(
        intent=CAP_CREATE_ENGINEERING_TASK, params={"title": "t", "body": "b"},
        source="ai_brain", confidence=0.3))
    assert rec is not None
    assert rec.status.value == "pending_approval"


def test_planner_availability_filter_rejects_when_no_provider():
    orch = _orchestrator()
    planner = ActionPlanner(orch, enabled=True, available_providers=["slack"])
    # engineering.create_task providers are github/jira/linear → none available → rejected
    rec = planner.submit(ObjectiveRequest(
        intent=CAP_CREATE_ENGINEERING_TASK, params={"title": "t", "body": "b"},
        source="ai_brain", confidence=0.9))
    assert rec is None
    assert planner.get_status()["rejected"] == 1


# ── Part XVI guard — capability names must be provider-agnostic (Article 4) ───

def test_capability_names_never_hardcode_a_provider():
    # Part XVI Art 3/4: a capability is an OBJECTIVE, never an application.
    # No capability name may contain a known vendor/provider/model token — the
    # Brain reasons "create an engineering task", never "create a GitHub issue".
    forbidden = {
        "github", "gitlab", "jira", "linear", "slack", "discord", "telegram",
        "whatsapp", "gdrive", "googledrive", "dropbox", "notion", "confluence",
        "aws", "azure", "gcp", "cloudflare", "openai", "anthropic", "llama",
        "deepseek", "qwen", "mistral", "composio", "polygon", "alphavantage",
    }
    reg = default_registry()
    for name in reg.names():
        tokens = set(name.lower().replace(".", "_").split("_"))
        leaked = tokens & forbidden
        assert not leaked, f"capability {name!r} hardcodes a provider: {leaked}"


def test_engineering_task_offers_interchangeable_providers():
    # Part XVI Art 6/8 — replacing a provider must be a registry-only change.
    cap = default_registry().get(CAP_CREATE_ENGINEERING_TASK)
    providers = {b.provider for b in cap.providers}
    assert {"github", "jira", "linear"} <= providers

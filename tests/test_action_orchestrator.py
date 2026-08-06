"""Offline tests for the Autonomous Action Layer (no network)."""

import json

import pytest

from action.composio import ComposioAdapter, MockActionAdapter, build_adapter
from action.orchestrator import (
    ActionObjective,
    ActionOrchestrator,
    ActionResult,
    ActionStatus,
    Decision,
    GovernancePolicy,
    RiskTier,
)


def _obj(**kw) -> ActionObjective:
    params = dict(
        capability="operator.notify",
        objective="notify operator of a SEV",
        source="ai_brain",
        confidence=0.9,
        risk_tier=RiskTier.LOW,
    )
    params.update(kw)
    return ActionObjective(**params)


def _policy(**kw) -> GovernancePolicy:
    params = dict(enabled=True, require_source=True, min_confidence=0.2,
                  auto_max_risk=RiskTier.LOW, medium_confidence_threshold=0.7)
    params.update(kw)
    return GovernancePolicy(**params)


# ── Governance policy (hierarchical autonomy) ─────────────────────────────────

def test_policy_reject_when_disabled():
    d, _ = GovernancePolicy(enabled=False).evaluate(_obj())
    assert d == Decision.REJECT


def test_policy_reject_without_source():
    d, r = _policy().evaluate(_obj(source=""))
    assert d == Decision.REJECT
    assert "Brain" in r


def test_policy_reject_below_confidence_floor():
    d, _ = _policy(min_confidence=0.5).evaluate(_obj(confidence=0.3))
    assert d == Decision.REJECT


def test_policy_authorizes_low_risk():
    assert _policy().evaluate(_obj(risk_tier=RiskTier.LOW))[0] == Decision.AUTHORIZE
    assert _policy().evaluate(_obj(risk_tier=RiskTier.NEGLIGIBLE))[0] == Decision.AUTHORIZE


def test_policy_medium_risk_needs_confidence_or_approval():
    assert _policy().evaluate(
        _obj(risk_tier=RiskTier.MEDIUM, confidence=0.8))[0] == Decision.AUTHORIZE
    assert _policy().evaluate(
        _obj(risk_tier=RiskTier.MEDIUM, confidence=0.5))[0] == Decision.REQUIRE_APPROVAL


def test_policy_high_and_destructive_always_require_approval():
    assert _policy().evaluate(
        _obj(risk_tier=RiskTier.HIGH, confidence=1.0))[0] == Decision.REQUIRE_APPROVAL
    assert _policy().evaluate(
        _obj(risk_tier=RiskTier.DESTRUCTIVE, confidence=1.0))[0] == Decision.REQUIRE_APPROVAL


def test_policy_auto_ceiling_capped_at_medium():
    # Even if an operator sets the auto ceiling to HIGH, it is capped at MEDIUM.
    p = _policy(auto_max_risk=RiskTier.HIGH)
    assert p.auto_max_risk == RiskTier.MEDIUM
    assert p.evaluate(_obj(risk_tier=RiskTier.HIGH, confidence=1.0))[0] == Decision.REQUIRE_APPROVAL


# ── Orchestrator lifecycle ────────────────────────────────────────────────────

class _FakeAdapter:
    name = "fake"

    def __init__(self, ok=True, raise_it=False):
        self.calls = []
        self._ok = ok
        self._raise = raise_it

    def execute(self, capability, params):
        self.calls.append((capability, dict(params)))
        if self._raise:
            raise RuntimeError("adapter boom")
        return ActionResult(ok=self._ok, external_ref="ext-1", detail="done", verified=self._ok)


def test_noop_is_skipped_without_adapter_call():
    a = _FakeAdapter()
    orch = ActionOrchestrator(_policy(), a)
    rec = orch.submit(_obj(capability="noop"))
    assert rec.status == ActionStatus.SKIPPED
    assert a.calls == []


def test_authorized_action_executes():
    a = _FakeAdapter(ok=True)
    orch = ActionOrchestrator(_policy(), a)
    rec = orch.submit(_obj(risk_tier=RiskTier.LOW))
    assert rec.status == ActionStatus.EXECUTED
    assert rec.result.external_ref == "ext-1"
    assert len(a.calls) == 1


def test_rejected_action_never_touches_adapter():
    a = _FakeAdapter()
    orch = ActionOrchestrator(GovernancePolicy(enabled=False), a)
    rec = orch.submit(_obj())
    assert rec.status == ActionStatus.REJECTED
    assert a.calls == []


def test_require_approval_parks_then_approve_executes():
    a = _FakeAdapter(ok=True)
    orch = ActionOrchestrator(_policy(), a)
    rec = orch.submit(_obj(risk_tier=RiskTier.HIGH, confidence=1.0))
    assert rec.status == ActionStatus.PENDING_APPROVAL
    assert a.calls == []
    approved = orch.approve(rec.objective.objective_id, approver="alice")
    assert approved.status == ActionStatus.EXECUTED
    assert len(a.calls) == 1


def test_adapter_fault_is_failed_not_crash():
    a = _FakeAdapter(raise_it=True)
    orch = ActionOrchestrator(_policy(), a)
    rec = orch.submit(_obj(risk_tier=RiskTier.LOW))
    assert rec.status == ActionStatus.FAILED
    assert rec.result is not None and rec.result.ok is False


def test_status_counts_and_no_secret_leak():
    a = _FakeAdapter(ok=True)
    orch = ActionOrchestrator(_policy(), a)
    orch.submit(_obj())
    orch.submit(_obj(capability="noop"))
    st = orch.get_status()
    assert st["status_counts"].get("executed") == 1
    assert st["status_counts"].get("skipped") == 1
    assert "api_key" not in json.dumps(st)


# ── Adapter selection + Composio request shaping ──────────────────────────────

class _Cfg:
    def __init__(self, enabled=False, dry_run=True, api_key="", base_url="https://backend.composio.dev",
                 entity_id="default", timeout_seconds=20.0):
        self.enabled = enabled
        self.dry_run = dry_run
        self.api_key = api_key
        self.base_url = base_url
        self.entity_id = entity_id
        self.timeout_seconds = timeout_seconds


def test_build_adapter_mock_unless_fully_live():
    assert isinstance(build_adapter(_Cfg(enabled=False)), MockActionAdapter)
    assert isinstance(build_adapter(_Cfg(enabled=True, dry_run=True, api_key="k")), MockActionAdapter)
    assert isinstance(build_adapter(_Cfg(enabled=True, dry_run=False, api_key="")), MockActionAdapter)
    live = build_adapter(_Cfg(enabled=True, dry_run=False, api_key="k"))
    assert isinstance(live, ComposioAdapter)


def _transport(status, obj):
    calls = []

    def _t(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": json.loads(body.decode())})
        return status, json.dumps(obj)

    _t.calls = calls
    return _t


def test_composio_adapter_request_shape_and_parse():
    t = _transport(200, {"successful": True, "id": "run_123"})
    a = ComposioAdapter("secret", base_url="https://backend.composio.dev",
                        entity_id="acct1", transport=t)
    res = a.execute("github.create_issue", {"title": "bug"})
    assert res.ok is True and res.external_ref == "run_123"
    call = t.calls[0]
    assert call["url"] == "https://backend.composio.dev/api/v2/actions/github.create_issue/execute"
    assert call["headers"]["x-api-key"] == "secret"
    assert call["body"] == {"entityId": "acct1", "input": {"title": "bug"}}


def test_composio_adapter_non_2xx_and_transport_error_fail_safe():
    a = ComposioAdapter("k", transport=_transport(500, {"error": "x"}))
    assert a.execute("cap", {}).ok is False

    def _boom(url, headers, body, timeout):
        raise RuntimeError("down")

    a2 = ComposioAdapter("k", transport=_boom)
    assert a2.execute("cap", {}).ok is False


def test_objective_defaults_and_id():
    o = ActionObjective(capability="x", source="ai_brain")
    assert o.objective_id  # auto-assigned
    assert o.risk_tier == RiskTier.LOW
    assert 0.0 <= o.confidence <= 1.0

"""Offline tests for the Model Manager (Part XVI, Article 9)."""

from llm.model_manager import (
    POLICY_PERFORMANCE,
    POLICY_PRIORITY,
    ModelManager,
    _Candidate,
    build_model_manager,
)


class _FakeClient:
    def __init__(self, model, *, usable=True, reply="ok", raises=False):
        self.model = model
        self.provider = "prov"
        self.usable = usable
        self._reply = reply
        self._raises = raises
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if self._raises:
            raise RuntimeError("model down")
        return self._reply


def _mgr(*cands, policy=POLICY_PRIORITY):
    return ModelManager(list(cands), policy=policy)


def test_priority_selects_lowest_priority_number_first():
    m = _mgr(_Candidate(_FakeClient("a"), priority=0),
             _Candidate(_FakeClient("b"), priority=1))
    assert m.complete("s", "u") == "ok"
    assert m.model == "a"


def test_failover_on_none_reply():
    m = _mgr(_Candidate(_FakeClient("a", reply=None), priority=0),
             _Candidate(_FakeClient("b", reply="ok2"), priority=1))
    assert m.complete("s", "u") == "ok2"
    assert m.model == "b"


def test_failover_on_exception():
    m = _mgr(_Candidate(_FakeClient("a", raises=True), priority=0),
             _Candidate(_FakeClient("b", reply="ok3"), priority=1))
    assert m.complete("s", "u") == "ok3"


def test_all_fail_returns_none():
    m = _mgr(_Candidate(_FakeClient("a", reply=None)),
             _Candidate(_FakeClient("b", reply=None)))
    assert m.complete("s", "u") is None


def test_no_usable_candidate_is_not_usable():
    m = _mgr(_Candidate(_FakeClient("a", usable=False)))
    assert m.usable is False
    assert m.complete("s", "u") is None


def test_unusable_candidate_is_skipped():
    m = _mgr(_Candidate(_FakeClient("a", usable=False), priority=0),
             _Candidate(_FakeClient("b", reply="okb"), priority=1))
    assert m.complete("s", "u") == "okb"


def test_performance_policy_prefers_working_model():
    bad = _Candidate(_FakeClient("a", reply=None), priority=0)
    good = _Candidate(_FakeClient("b", reply="okb"), priority=1)
    m = _mgr(bad, good, policy=POLICY_PERFORMANCE)
    m.complete("s", "u")   # a fails, b succeeds
    m.complete("s", "u")
    ordered = [c.model for c in m._ordered()]
    assert ordered[0] == "b"


def test_describe_reports_roster_and_policy():
    m = _mgr(_Candidate(_FakeClient("a"), priority=0))
    m.complete("s", "u")
    d = m.describe()
    assert d["manager"] is True
    assert d["policy"] == POLICY_PRIORITY
    assert d["candidate_count"] == 1
    assert d["selected_model"] == "a"


# ── build_model_manager (from an LLMConfig-like object) ───────────────────────

class _Cfg:
    def __init__(self, **kw):
        self.provider = kw.get("provider", "openai")
        self.model = kw.get("model", "gpt-x")
        self.api_key = kw.get("api_key", "k")
        self.base_url = kw.get("base_url", "")
        self.timeout_seconds = 20.0
        self.max_tokens = 512
        self.temperature = 0.2
        self.extra_models = kw.get("extra_models", [])
        self.model_policy = kw.get("model_policy", "priority")


def test_build_manager_single_primary():
    m = build_model_manager(_Cfg(provider="openai", model="gpt-x", api_key="k"))
    assert m is not None and m.usable
    assert m.describe()["candidate_count"] == 1


def test_build_manager_with_extra_models_inherits_key():
    # A gateway (base_url) fronting several models under one key.
    cfg = _Cfg(provider="agentrouter", model="m1", api_key="k",
               base_url="https://gw.example/v1",
               extra_models=[{"provider": "agentrouter", "model": "m2", "priority": 2}])
    m = build_model_manager(cfg)
    assert m is not None
    d = m.describe()
    assert d["candidate_count"] == 2
    assert d["usable_count"] == 2          # extra inherits base_url + key


def test_build_manager_none_when_unusable():
    # gateway provider with no base_url → not usable → None
    assert build_model_manager(_Cfg(provider="agentrouter", model="m", api_key="k",
                                    base_url="")) is None

"""Offline tests for the Reasoning Orchestrator (Constitution Part XVII)."""

from types import SimpleNamespace

from llm.reasoning_orchestrator import (
    EngineOpinion,
    ReasoningEngine,
    ReasoningOrchestrator,
    build_reasoning_orchestrator,
)


class _FakeReasoner:
    """Duck-typed reasoner: .available + .reason(symbol, evidence, now)."""

    def __init__(self, direction="LONG", confidence=0.7, available=True,
                 raises=False, none=False):
        self._d = direction
        self._c = confidence
        self.available = available
        self._raises = raises
        self._none = none
        self.calls = 0

    def reason(self, symbol, evidence, now=None):
        self.calls += 1
        if self._raises:
            raise RuntimeError("engine down")
        if self._none:
            return None
        return SimpleNamespace(direction=self._d, confidence=self._c, rationale="because")


def _engine(name, **kw):
    return ReasoningEngine(name, _FakeReasoner(**kw), capabilities=kw.pop("capabilities", []))


# ── ReasoningEngine ────────────────────────────────────────────────────────────

def test_engine_consult_returns_opinion_and_records_health():
    eng = ReasoningEngine("openai", _FakeReasoner(direction="SHORT", confidence=0.6))
    op = eng.consult("EURUSD", {})
    assert isinstance(op, EngineOpinion)
    assert op.engine == "openai" and op.direction == "SHORT" and op.confidence == 0.6
    assert eng.calls == 1 and eng.reliability == 1.0


def test_engine_fault_is_recorded_not_raised():
    eng = ReasoningEngine("bad", _FakeReasoner(raises=True))
    assert eng.consult("EURUSD", {}) is None
    assert eng.faults == 1 and eng.reliability == 0.0


def test_engine_unavailable_returns_none():
    eng = ReasoningEngine("off", _FakeReasoner(available=False))
    assert eng.consult("EURUSD", {}) is None


def test_engine_capability_match():
    eng = ReasoningEngine("risk", _FakeReasoner(), capabilities=["risk", "research"])
    assert eng.has_capability("risk") is True
    assert eng.has_capability("code") is False
    assert eng.has_capability("") is True          # no requirement matches all


# ── ReasoningOrchestrator: fan-out, NO vote ───────────────────────────────────

def test_consult_collects_every_opinion_no_vote():
    orch = ReasoningOrchestrator([
        ReasoningEngine("a", _FakeReasoner(direction="LONG", confidence=0.8)),
        ReasoningEngine("b", _FakeReasoner(direction="SHORT", confidence=0.6)),
        ReasoningEngine("c", _FakeReasoner(direction="LONG", confidence=0.5)),
    ], max_engines=5)
    con = orch.consult("EURUSD", {})
    # All three opinions are returned as-is — no aggregate/majority direction.
    assert len(con.opinions) == 3
    assert {o.engine for o in con.opinions} == {"a", "b", "c"}
    assert not hasattr(con, "direction")           # the consultation never decides


def test_consult_skips_faulty_engine_but_returns_others():
    orch = ReasoningOrchestrator([
        ReasoningEngine("ok", _FakeReasoner(direction="LONG")),
        ReasoningEngine("bad", _FakeReasoner(raises=True)),
    ], max_engines=5)
    con = orch.consult("EURUSD", {})
    assert [o.engine for o in con.opinions] == ["ok"]


def test_consult_capped_by_max_engines():
    orch = ReasoningOrchestrator(
        [ReasoningEngine(f"e{i}", _FakeReasoner()) for i in range(5)], max_engines=2)
    con = orch.consult("EURUSD", {})
    assert len(con.consulted) == 2


def test_select_prefers_capability_then_falls_back():
    a = ReasoningEngine("a", _FakeReasoner(), capabilities=["risk"])
    b = ReasoningEngine("b", _FakeReasoner(), capabilities=["code"])
    orch = ReasoningOrchestrator([a, b], max_engines=5)
    # Requesting a declared capability narrows to matching engines.
    assert [e.name for e in orch.select(capability="risk")] == ["a"]
    # Requesting an undeclared capability falls back to all available engines.
    assert {e.name for e in orch.select(capability="unknown")} == {"a", "b"}


def test_reliability_provider_orders_selection():
    a = ReasoningEngine("a", _FakeReasoner())
    b = ReasoningEngine("b", _FakeReasoner())
    # 'b' is measured more useful → selected first.
    prov = lambda src: 1.5 if src == "reasoning_engine.b" else 0.5
    orch = ReasoningOrchestrator([a, b], max_engines=1, reliability_provider=prov)
    assert [e.name for e in orch.select()] == ["b"]


def test_orchestrator_unavailable_when_no_engine_available():
    orch = ReasoningOrchestrator([ReasoningEngine("off", _FakeReasoner(available=False))])
    assert orch.available is False
    assert orch.consult("EURUSD", {}).opinions == []


def test_status_reports_engines():
    orch = ReasoningOrchestrator([ReasoningEngine("a", _FakeReasoner())])
    orch.consult("EURUSD", {})
    st = orch.get_status()
    assert st["engine_count"] == 1 and st["consultations"] == 1
    assert st["engines"][0]["name"] == "a"


# ── build_reasoning_orchestrator (from an LLMConfig-like object) ──────────────

class _Cfg:
    def __init__(self, **kw):
        self.provider = kw.get("provider", "openai")
        self.model = kw.get("model", "gpt-x")
        self.api_key = kw.get("api_key", "k")
        self.base_url = kw.get("base_url", "")
        self.timeout_seconds = 20.0
        self.max_tokens = 512
        self.temperature = 0.2
        self.min_interval_seconds = 30.0
        self.drive_decisions = False
        self.extra_models = kw.get("extra_models", [])
        self.consult_max_engines = kw.get("consult_max_engines", 3)


def test_build_orchestrator_multi_engine():
    cfg = _Cfg(provider="agentrouter", model="m1", api_key="k",
               base_url="https://gw/v1",
               extra_models=[{"provider": "agentrouter", "model": "m2",
                              "capabilities": ["risk"]}])
    orch = build_reasoning_orchestrator(cfg)
    assert orch is not None
    st = orch.get_status()
    assert st["engine_count"] == 2 and st["available_engines"] == 2


def test_build_orchestrator_none_when_no_usable_engine():
    # gateway provider with no base_url → no usable engine → None
    assert build_reasoning_orchestrator(_Cfg(provider="agentrouter", model="m",
                                             api_key="k", base_url="")) is None

"""Tests for the Composio knowledge source (Constitution Part IX v3.0)."""

from cognition.knowledge_source import KnowledgeSource


class _Clock:
    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t


class _Adapter:
    """Minimal adapter double — returns a fixed payload, records calls."""

    def __init__(self, payload=None, ok=True, usable=True):
        self._payload = payload if payload is not None else {"items": [
            {"title": "ctx", "snippet": "external read", "sentiment": "bullish"}]}
        self._ok = ok
        self._usable = usable
        self.calls = []

    @property
    def usable(self):
        return self._usable

    def execute(self, capability, params):
        self.calls.append({"capability": capability, "params": dict(params or {})})
        from action.orchestrator import ActionResult
        return ActionResult(ok=self._ok, data=dict(self._payload))


def test_disabled_source_yields_nothing():
    ks = KnowledgeSource(_Adapter(), enabled=False)
    assert ks.evidence_for("XAUUSD") == []
    assert ks.get_status()["queries"] == 0


def test_enabled_source_produces_macro_evidence():
    ks = KnowledgeSource(_Adapter(), enabled=True, interval_seconds=0.0)
    ev = ks.evidence_for("XAUUSD")
    assert len(ev) == 1
    assert ev[0].symbol == "XAUUSD"
    st = ks.get_status()
    assert st["queries"] == 1 and st["hits"] == 1 and st["evidence_produced"] == 1


def test_per_symbol_throttle():
    clk = _Clock(1000.0)
    ks = KnowledgeSource(_Adapter(), enabled=True, interval_seconds=300.0, clock=clk)
    assert len(ks.evidence_for("XAUUSD")) == 1     # first call fires
    assert ks.evidence_for("XAUUSD") == []         # within interval ⇒ throttled
    assert ks.get_status()["throttled"] == 1
    clk.t += 301.0
    assert len(ks.evidence_for("XAUUSD")) == 1     # interval elapsed ⇒ fires again


def test_advisor_path_adds_reasoning_evidence():
    adapter = _Adapter(payload={
        "answer": "mixed", "direction": "neutral", "confidence": 0.4})
    ks = KnowledgeSource(adapter, enabled=True, interval_seconds=0.0, advisor_enabled=True)
    ev = ks.evidence_for("XAUUSD")
    # knowledge cap + advisor cap ⇒ two adapter calls
    assert len(adapter.calls) == 2
    assert any(e.domain.value == "reasoning" for e in ev)


def test_fault_is_safe_and_counted():
    class _Boom:
        usable = True
        def execute(self, capability, params):
            raise RuntimeError("network down")
    ks = KnowledgeSource(_Boom(), enabled=True, interval_seconds=0.0)
    assert ks.evidence_for("XAUUSD") == []   # must not raise
    assert ks.get_status()["faults"] == 1


def test_not_usable_adapter_is_disabled():
    ks = KnowledgeSource(_Adapter(usable=False), enabled=True, interval_seconds=0.0)
    assert ks.enabled is False
    assert ks.evidence_for("XAUUSD") == []


def test_end_to_end_with_real_mock_adapter():
    # The shipped MockActionAdapter must return knowledge data so the whole
    # path is exercisable offline / in dry-run.
    from action.composio import MockActionAdapter
    ks = KnowledgeSource(MockActionAdapter(), enabled=True, interval_seconds=0.0,
                         advisor_enabled=True)
    ev = ks.evidence_for("XAUUSD")
    assert len(ev) >= 2   # one research item + one advisor answer
    domains = {e.domain.value for e in ev}
    assert "macro" in domains and "reasoning" in domains

"""Offline tests for the LLM reasoning worker (no thread timing dependence)."""

from llm.worker import LLMReasoningWorker


class _StubReasoner:
    """Records reason() calls; ``available`` and per-symbol behaviour tunable."""

    def __init__(self, available=True, raise_on=None, none_on=None):
        self._available = available
        self._raise_on = set(raise_on or [])
        self._none_on = set(none_on or [])
        self.calls = []

    @property
    def available(self):
        return self._available

    def reason(self, symbol, evidence, now=None):
        self.calls.append((symbol, evidence, now))
        if symbol in self._raise_on:
            raise RuntimeError("boom")
        if symbol in self._none_on:
            return None
        return {"symbol": symbol, "direction": "LONG"}


def _source(mapping):
    return lambda: dict(mapping)


def test_run_once_calls_reason_per_symbol():
    r = _StubReasoner()
    w = LLMReasoningWorker(r, _source({"EURUSD": {"a": 1}, "GBPUSD": {"b": 2}}))
    formed = w.run_once(now=123.0)
    assert formed == 2
    assert {c[0] for c in r.calls} == {"EURUSD", "GBPUSD"}
    assert r.calls[0][2] == 123.0  # clock threaded through


def test_run_once_noop_when_unavailable():
    r = _StubReasoner(available=False)
    w = LLMReasoningWorker(r, _source({"EURUSD": {}}))
    assert w.run_once() == 0
    assert r.calls == []


def test_run_once_fail_safe_when_source_raises():
    def _boom():
        raise RuntimeError("source down")

    r = _StubReasoner()
    w = LLMReasoningWorker(r, _boom)
    assert w.run_once() == 0
    assert r.calls == []


def test_run_once_caps_symbols_per_cycle():
    r = _StubReasoner()
    src = {f"S{i}": {} for i in range(20)}
    w = LLMReasoningWorker(r, _source(src), max_symbols_per_cycle=3)
    assert w.run_once() == 3
    assert len(r.calls) == 3


def test_one_symbol_fault_does_not_stop_loop():
    r = _StubReasoner(raise_on=["RAISE"])
    w = LLMReasoningWorker(r, _source({"RAISE": {}, "OK": {}}))
    formed = w.run_once()
    assert formed == 1                      # OK formed; RAISE swallowed
    assert {c[0] for c in r.calls} == {"RAISE", "OK"}


def test_none_opinions_not_counted():
    r = _StubReasoner(none_on=["Q"])
    w = LLMReasoningWorker(r, _source({"Q": {}, "R": {}}))
    assert w.run_once() == 1


def test_start_is_noop_when_unavailable():
    r = _StubReasoner(available=False)
    w = LLMReasoningWorker(r, _source({"EURUSD": {}}))
    w.start()
    assert w.running is False
    w.stop()  # safe even though never started


def test_start_stop_toggles_running():
    r = _StubReasoner(available=True)
    w = LLMReasoningWorker(r, _source({"EURUSD": {}}), interval_seconds=1.0)
    w.start()
    assert w.running is True
    w.stop()
    assert w.running is False


def test_get_status_shape():
    r = _StubReasoner()
    w = LLMReasoningWorker(r, _source({"EURUSD": {}}))
    w.run_once()
    s = w.get_status()
    assert s["available"] is True
    assert s["cycles"] == 1
    assert s["opinions_formed"] == 1

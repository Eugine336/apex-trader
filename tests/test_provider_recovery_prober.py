"""Background recovery prober — a benched (circuit-OPEN) advisor is retried
quietly OFF the consult path so it rejoins the council the moment it heals.

The prober's judgement has two halves:

* WHICH benched advisors to retry — only those whose last failure was
  *transient* (5xx / gateway timeout / connection). Quota/credit/auth faults
  (HTTP 401/402/403/429) do not clear by retrying, so they are left to their
  own reset clock and never background-probed.
* HOW OFTEN — at most once per recovery interval, gated per engine.

These tests drive the REAL :class:`ReasoningEngine` / :class:`ReasoningOrchestrator`
(only the completion client + reasoner are faked) so the circuit-breaker wiring,
the probe call, and the interval gate are all exercised end to end. Pure stdlib.
"""

from llm.health import is_transient_failure
from llm.reasoning_orchestrator import ReasoningEngine, ReasoningOrchestrator


# ── fakes ────────────────────────────────────────────────────────────────────

class _FakeProbeClient:
    """Minimal completion client: counts ``complete`` calls and reports a
    configurable last-failure signature (what the prober classifies on)."""

    def __init__(self, reply="READY", fail_signature=""):
        self._reply = reply
        self.last_fail_signature = fail_signature
        self.complete_calls = 0

    def complete(self, system, user):
        self.complete_calls += 1
        return self._reply


class _ProbeReasoner:
    """Duck-typed reasoner exposing ``available`` + ``client`` (the two things
    the prober reaches through). ``reason`` is unused on the recovery path."""

    def __init__(self, client, available=True):
        self.client = client
        self.available = available

    def reason(self, symbol, evidence, now=None):  # pragma: no cover - unused here
        return None


def _open_breaker(engine):
    """Trip the engine's circuit OPEN with the default 3 consecutive failures.

    The real breaker uses ``time.monotonic`` with a 30s base cooldown, so once
    tripped it stays OPEN for the whole (sub-second) test — recovery decisions
    read the breaker's real clock, while interval gating uses the injected
    ``now`` we pass to ``recover_once``."""
    for _ in range(3):
        engine.breaker.record_failure()
    assert engine.breaker.is_open() is True


# ── failure classifier ─────────────────────────────────────────────────────────

def test_quota_auth_signatures_are_not_transient():
    for sig in ("http:401", "http:402", "http:403", "http:429"):
        assert is_transient_failure(sig) is False
    # case / whitespace insensitive
    assert is_transient_failure("HTTP:429") is False
    assert is_transient_failure("  http:402  ") is False


def test_transient_and_unknown_signatures_are_transient():
    for sig in ("http:500", "http:502", "http:503", "http:504",
                "transport:TimeoutError", "transport:ConnectionError"):
        assert is_transient_failure(sig) is True
    # An empty / unknown signature errs toward retrying.
    assert is_transient_failure("") is True
    assert is_transient_failure(None) is True  # type: ignore[arg-type]


# ── recover_once: WHICH engines get probed ─────────────────────────────────────

def test_transient_benched_advisor_is_probed_and_rejoins():
    client = _FakeProbeClient(reply="READY", fail_signature="http:504")
    eng = ReasoningEngine("gw", _ProbeReasoner(client))
    _open_breaker(eng)

    orch = ReasoningOrchestrator([eng], recovery_interval=60.0)
    recovered = orch.recover_once()

    assert client.complete_calls == 1          # it WAS probed
    assert recovered == 1                       # and it answered
    assert eng.breaker.admits() is True         # circuit cleared → rejoined


def test_quota_benched_advisor_is_not_probed():
    client = _FakeProbeClient(reply="READY", fail_signature="http:429")
    eng = ReasoningEngine("rate", _ProbeReasoner(client))
    _open_breaker(eng)

    orch = ReasoningOrchestrator([eng], recovery_interval=60.0)
    recovered = orch.recover_once()

    assert client.complete_calls == 0           # left to its own reset clock
    assert recovered == 0
    assert eng.breaker.is_open() is True         # still benched


def test_healthy_advisor_is_not_probed():
    client = _FakeProbeClient(reply="READY", fail_signature="")
    eng = ReasoningEngine("ok", _ProbeReasoner(client))
    assert eng.breaker.is_open() is False        # closed from the start

    orch = ReasoningOrchestrator([eng], recovery_interval=60.0)
    recovered = orch.recover_once()

    assert client.complete_calls == 0            # nothing to recover
    assert recovered == 0


def test_failed_probe_leaves_advisor_benched():
    client = _FakeProbeClient(reply=None, fail_signature="http:500")
    eng = ReasoningEngine("down", _ProbeReasoner(client))
    _open_breaker(eng)

    orch = ReasoningOrchestrator([eng], recovery_interval=60.0)
    recovered = orch.recover_once()

    assert client.complete_calls == 1            # probed
    assert recovered == 0                        # but still down
    assert eng.breaker.is_open() is True          # stays benched


# ── recover_once: HOW OFTEN (per-engine interval gate) ─────────────────────────

def test_recovery_is_interval_gated_per_engine():
    # A failing probe keeps the breaker OPEN so every eligible sweep would probe
    # were it not for the interval gate; the injected ``now`` drives the gate.
    client = _FakeProbeClient(reply=None, fail_signature="http:503")
    eng = ReasoningEngine("slow", _ProbeReasoner(client))
    _open_breaker(eng)

    orch = ReasoningOrchestrator([eng], recovery_interval=60.0)

    orch.recover_once(now=0.0)      # first sweep — always probes
    assert client.complete_calls == 1
    orch.recover_once(now=30.0)     # 30s < 60s interval — skipped
    assert client.complete_calls == 1
    orch.recover_once(now=70.0)     # 70s >= 60s — probes again
    assert client.complete_calls == 2


# ── start/stop plumbing ─────────────────────────────────────────────────────────

def test_start_recovery_zero_interval_is_a_noop():
    client = _FakeProbeClient()
    eng = ReasoningEngine("x", _ProbeReasoner(client))
    orch = ReasoningOrchestrator([eng], recovery_interval=0.0)

    orch.start_recovery()                       # disabled → no thread
    assert orch._recovery_thread is None

    orch.start_recovery(interval=0)             # explicit 0 also a no-op
    assert orch._recovery_thread is None


def test_start_recovery_is_idempotent_and_stoppable():
    client = _FakeProbeClient()
    eng = ReasoningEngine("x", _ProbeReasoner(client))
    orch = ReasoningOrchestrator([eng], recovery_interval=30.0)

    orch.start_recovery()
    first = orch._recovery_thread
    assert first is not None and first.is_alive()

    orch.start_recovery()                       # second call is a no-op
    assert orch._recovery_thread is first

    orch.stop_recovery()
    first.join(timeout=2.0)
    assert first.is_alive() is False

"""GPU/Compute Constitution — circuit breaker, health gating, local concurrency,
and compute-class routing.

Covers:
* CircuitBreaker: trip after N consecutive failures, OPEN rejects, HALF_OPEN
  after cooldown, close on success, exponential backoff capped, reset on success.
* ConcurrencyLimiter: unbounded no-op; bounded blocks when exhausted.
* ModelManager: a tripped provider is skipped (not re-hammered); compute-class
  and context-window filtering; class filter never starves.
* ReasoningOrchestrator (council): local advisors share a concurrency slot (fix
  for "all Ollama on → CPU outage"); a failing advisor is circuit-broken out of
  the panel; a throttled (healthy) advisor is NOT circuit-broken.
"""

import threading
import time

from llm.health import CircuitBreaker, CircuitConfig, ConcurrencyLimiter
from llm.model_manager import ModelManager, POLICY_PRIORITY, _Candidate
from llm.reasoning_orchestrator import ReasoningEngine, ReasoningOrchestrator


class _Clock:
    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


# ── CircuitBreaker ────────────────────────────────────────────────────────────

def test_breaker_trips_then_halfopen_then_closes():
    clk = _Clock()
    cb = CircuitBreaker(CircuitConfig(3, 30.0, 300.0), clock=clk)
    assert cb.admits() and cb.state() == "closed"
    cb.record_failure()
    cb.record_failure()
    assert cb.admits()                       # 2 < 3 — still closed
    cb.record_failure()
    assert not cb.admits() and cb.state() == "open"   # tripped
    clk.t += 31                              # cooldown elapsed
    assert cb.admits() and cb.state() == "half_open"
    cb.record_success()
    assert cb.admits() and cb.state() == "closed"


def test_breaker_backoff_grows_and_caps():
    clk = _Clock()
    cb = CircuitBreaker(CircuitConfig(1, 10.0, 25.0), clock=clk)
    cb.record_failure()                      # trip 1 → 10s
    assert cb.seconds_until_retry() == 10.0
    clk.t += 11
    cb.record_failure()                      # trip 2 → 20s
    assert cb.seconds_until_retry() == 20.0
    clk.t += 21
    cb.record_failure()                      # trip 3 → 40s capped to 25s
    assert cb.seconds_until_retry() == 25.0


def test_breaker_success_resets_consecutive():
    cb = CircuitBreaker(CircuitConfig(3, 30.0, 300.0))
    cb.record_failure()
    cb.record_failure()
    cb.record_success()                      # resets the streak
    cb.record_failure()
    cb.record_failure()
    assert cb.admits()                       # only 2 consecutive after the reset


# ── ConcurrencyLimiter ──────────────────────────────────────────────────────

def test_limiter_unbounded_is_noop():
    with ConcurrencyLimiter(0).slot() as got:
        assert got is True


def test_limiter_bounded_blocks_when_exhausted():
    lim = ConcurrencyLimiter(1)
    with lim.slot() as g1:
        assert g1 is True
        with lim.slot(timeout=0.05) as g2:
            assert g2 is False               # exhausted
    with lim.slot(timeout=0.05) as g3:       # released
        assert g3 is True


# ── ModelManager routing ─────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self, reply, *, model="m", provider="p"):
        self._reply = reply
        self.usable = True
        self.model = model
        self.provider = provider
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        return self._reply


def _cand(client, *, tier=1, priority=0, classes=frozenset(), ctx=0, threshold=1):
    return _Candidate(
        client=client, tier=tier, priority=priority, classes=classes,
        context_window=ctx,
        breaker=CircuitBreaker(CircuitConfig(threshold, 300.0, 300.0)),
    )


def test_manager_circuit_skips_dead_provider():
    a = _FakeClient(None, model="A")         # always fails
    b = _FakeClient("ok", model="B")
    mgr = ModelManager([_cand(a, priority=0, threshold=1), _cand(b, priority=1)],
                       policy=POLICY_PRIORITY)
    assert mgr.complete("s", "u") == "ok"
    assert mgr.complete("s", "u") == "ok"
    assert mgr.complete("s", "u") == "ok"
    assert a.calls == 1                      # tried once, then circuit-open → skipped
    assert b.calls == 3


def test_manager_compute_class_filters():
    fast = _FakeClient("fast", model="F")
    deep = _FakeClient("deep", model="D")
    mgr = ModelManager([_cand(fast, priority=0, classes=frozenset({"fast"})),
                        _cand(deep, priority=1, classes=frozenset({"deep"}))],
                       policy=POLICY_PRIORITY)
    assert mgr.complete("s", "u", compute_class="deep") == "deep"
    assert mgr.complete("s", "u", compute_class="fast") == "fast"


def test_manager_context_window_filters():
    small = _FakeClient("small", model="S")
    big = _FakeClient("big", model="B")
    mgr = ModelManager([_cand(small, priority=0, ctx=1000),
                        _cand(big, priority=1, ctx=32000)], policy=POLICY_PRIORITY)
    assert mgr.complete("s", "u", min_context=8000) == "big"


def test_manager_class_filter_never_starves():
    only = _FakeClient("only", model="O")
    mgr = ModelManager([_cand(only, priority=0, classes=frozenset({"fast"}))],
                       policy=POLICY_PRIORITY)
    # Request "deep" but only a fast model exists → fall back rather than starve.
    assert mgr.complete("s", "u", compute_class="deep") == "only"


# ── Council: local concurrency + circuit breaker ─────────────────────────────

class _Tracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.cur = 0
        self.max = 0


class _ConcReasoner:
    available = True

    def __init__(self, tracker):
        self._t = tracker

    def reason(self, symbol, evidence, now=None):
        with self._t.lock:
            self._t.cur += 1
            self._t.max = max(self._t.max, self._t.cur)
        time.sleep(0.05)
        with self._t.lock:
            self._t.cur -= 1
        return type("O", (), {"direction": "LONG", "confidence": 0.8, "rationale": "x"})()

    def last_reason_degraded(self, symbol):
        return False


def test_council_local_models_share_one_slot():
    tracker = _Tracker()
    lim = ConcurrencyLimiter(1)
    engines = [
        ReasoningEngine(f"local{i}", _ConcReasoner(tracker), is_local=True,
                        local_limiter=lim, local_acquire_timeout=5.0)
        for i in range(3)
    ]
    orch = ReasoningOrchestrator(engines, max_engines=10, panel=True)
    res = orch.consult("EURUSD", {}, max_engines=0)   # consult ALL
    assert len(res.opinions) == 3                     # all still advise
    assert tracker.max == 1                           # never more than 1 local at once


class _FailReasoner:
    available = True

    def reason(self, symbol, evidence, now=None):
        return None

    def last_reason_degraded(self, symbol):
        return True                                   # a REAL failure


class _ThrottleReasoner:
    available = True

    def reason(self, symbol, evidence, now=None):
        return None

    def last_reason_degraded(self, symbol):
        return False                                  # benign throttle


def test_council_circuit_breaks_failing_advisor():
    eng = ReasoningEngine("x", _FailReasoner(),
                          circuit=CircuitConfig(2, 300.0, 300.0))
    assert eng.available
    eng.consult("EURUSD", {})                         # fail 1
    assert eng.available
    eng.consult("EURUSD", {})                         # fail 2 → trip
    assert not eng.available                          # dropped from the panel


def test_council_throttle_does_not_circuit_break():
    eng = ReasoningEngine("t", _ThrottleReasoner(),
                          circuit=CircuitConfig(1, 300.0, 300.0))
    eng.consult("EURUSD", {})
    eng.consult("EURUSD", {})
    assert eng.available                              # a throttle never trips it

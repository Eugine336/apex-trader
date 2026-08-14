"""Provider quota recovery — honour a vendor-signalled cooldown (Constitution §27).

A free-tier LLM provider that returns HTTP 429 tells us, via ``Retry-After`` /
``x-ratelimit-reset*`` headers, exactly when its quota returns. APEX must record
that reset and bench the provider until then instead of blindly re-hammering it
every cycle (which only deepens the throttle and burns the quota).

Covers:
* Header parsing (``_parse_retry_after`` / ``_retry_after_value`` /
  ``_parse_duration_token``): delta-seconds, HTTP-date, compound duration
  tokens, max-across-headers, and fail-safe 0.0 on garbage.
* ``CircuitBreaker.bench``: opens the circuit until the signalled reset WITHOUT
  counting a hard fault (a throttle is not an outage); only extends, never
  shortens; ignores non-positive; caps at 24h; cleared by a success.
* ``LLMClient.complete``: captures the cooldown from a 3-tuple transport,
  still works with a 2-tuple transport, and clears a stale cooldown on success.
* Council: a throttled advisor that signals a reset is benched off the panel
  without being counted as failing.

Pure stdlib; no network.
"""

import time
from email.utils import formatdate

from llm.client import LLMClient, _parse_duration_token, _parse_retry_after, _retry_after_value
from llm.health import CircuitBreaker, CircuitConfig
from llm.reasoning_orchestrator import ReasoningEngine


class _Clock:
    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


# ── header parsing ─────────────────────────────────────────────────────────────


def test_parse_retry_after_delta_seconds():
    assert _parse_retry_after({"retry-after": "30"}) == 30.0
    assert _parse_retry_after({"retry-after": "0"}) == 0.0


def test_parse_retry_after_http_date_is_future_delta():
    future = formatdate(time.time() + 60, usegmt=True)
    val = _parse_retry_after({"retry-after": future})
    assert 45.0 <= val <= 75.0


def test_parse_ratelimit_reset_duration_tokens():
    assert _parse_retry_after({"x-ratelimit-reset-requests": "6m0s"}) == 360.0
    assert _parse_retry_after({"x-ratelimit-reset-tokens": "1m30s"}) == 90.0
    assert _parse_retry_after({"x-ratelimit-reset": "500ms"}) == 0.5


def test_parse_retry_after_takes_max_and_ignores_garbage():
    hdrs = {
        "retry-after": "10",
        "x-ratelimit-reset-requests": "45",
        "x-ratelimit-reset-tokens": "garbage",
    }
    assert _parse_retry_after(hdrs) == 45.0
    assert _parse_retry_after({}) == 0.0
    assert _parse_retry_after({"retry-after": "notanumber"}) == 0.0
    assert _parse_retry_after(None) == 0.0  # type: ignore[arg-type]


def test_parse_duration_token_forms():
    assert _parse_duration_token("45") == 45.0
    assert _parse_duration_token("2.5s") == 2.5
    assert _parse_duration_token("1h2m3s") == 3723.0
    assert _parse_duration_token("") == 0.0
    assert _parse_duration_token("garbage") == 0.0


def test_retry_after_value_handles_bare_seconds_and_garbage():
    assert _retry_after_value("12") == 12.0
    assert _retry_after_value("") == 0.0
    assert _retry_after_value("not-a-date") == 0.0


# ── CircuitBreaker.bench ────────────────────────────────────────────────────────


def test_bench_opens_until_signalled_reset_without_fault():
    clk = _Clock()
    cb = CircuitBreaker(CircuitConfig(3, 30.0, 300.0), clock=clk)
    assert cb.admits()
    cb.bench(120.0)
    assert not cb.admits()  # benched
    assert cb.seconds_until_retry() == 120.0
    assert cb.failures == 0  # a throttle is NOT a hard fault
    assert cb.success_rate == 1.0
    clk.t += 121
    assert cb.admits()  # reset elapsed → eligible again


def test_bench_only_extends_never_shortens():
    clk = _Clock()
    cb = CircuitBreaker(CircuitConfig(3, 30.0, 300.0), clock=clk)
    cb.bench(200.0)
    cb.bench(50.0)  # shorter — ignored
    assert cb.seconds_until_retry() == 200.0


def test_bench_ignores_nonpositive_and_caps_at_24h():
    clk = _Clock()
    cb = CircuitBreaker(CircuitConfig(3, 30.0, 300.0), clock=clk)
    cb.bench(0.0)
    cb.bench(-5.0)
    cb.bench("garbage")  # type: ignore[arg-type]
    assert cb.admits()  # nothing benched
    cb.bench(10_000_000.0)  # absurd → capped to 24h
    assert cb.seconds_until_retry() == 86400.0


def test_bench_cleared_by_success():
    cb = CircuitBreaker(CircuitConfig(3, 30.0, 300.0))
    cb.bench(300.0)
    assert not cb.admits()
    cb.record_success()
    assert cb.admits()
    assert cb.to_dict()["signaled_cooldown_s"] == 0.0


# ── LLMClient.complete ──────────────────────────────────────────────────────────


def _client(transport):
    return LLMClient(provider="openai", model="gpt-x", api_key="k", transport=transport)


def test_client_captures_retry_after_on_429():
    def tr(url, headers, body, timeout):
        return (429, "rate limited", {"retry-after": "30"})

    c = _client(tr)
    assert c.complete("s", "u") is None
    assert c.last_retry_after_seconds == 30.0


def test_client_two_tuple_transport_still_works():
    def tr(url, headers, body, timeout):
        return (200, '{"choices":[{"message":{"content":"ok"}}]}')

    c = _client(tr)
    assert c.complete("s", "u") == "ok"
    assert c.last_retry_after_seconds == 0.0


def test_client_success_clears_prior_cooldown():
    seq = [
        (429, "rl", {"retry-after": "45"}),
        (200, '{"choices":[{"message":{"content":"ok"}}]}'),
    ]

    def tr(url, headers, body, timeout):
        return seq.pop(0)

    c = _client(tr)
    assert c.complete("s", "u") is None
    assert c.last_retry_after_seconds == 45.0
    assert c.complete("s", "u") == "ok"
    assert c.last_retry_after_seconds == 0.0


def test_client_429_without_headers_is_zero():
    def tr(url, headers, body, timeout):
        return (429, "rl")  # 2-tuple — no header info

    c = _client(tr)
    assert c.complete("s", "u") is None
    assert c.last_retry_after_seconds == 0.0


# ── Council benches a throttled advisor without counting a fault ─────────────────


class _ThrottleClient:
    def __init__(self, retry_after):
        self.last_retry_after_seconds = float(retry_after)
        self.last_fail_signature = "http:429"

    def complete(self, system, user):  # pragma: no cover - unused here
        return None


class _ThrottleRetryReasoner:
    available = True

    def __init__(self, client):
        self.client = client

    def reason(self, symbol, evidence, now=None):
        return None  # throttled — no opinion

    def last_reason_degraded(self, symbol):
        return False  # not an outage, just rate-limited


def test_council_throttle_with_retry_after_benches_without_fault():
    eng = ReasoningEngine(
        "rl",
        _ThrottleRetryReasoner(_ThrottleClient(120.0)),
        circuit=CircuitConfig(3, 30.0, 300.0),
    )
    assert eng.available
    assert eng.consult("EURUSD", {}) is None
    # Benched until the vendor-signalled reset, even though the fault threshold
    # (3) was never reached and no hard fault was recorded.
    assert eng.breaker.is_open() is True
    assert eng.breaker.failures == 0
    assert eng.breaker.seconds_until_retry() >= 100.0
    assert eng.available is False

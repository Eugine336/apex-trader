"""Provider budget accounting — finite free-tier quotas (Constitution §22–§28).

APEX must know a provider's request/token limits, meter consumption, and refuse
to spend a request that would exceed a scarce free tier — benching the provider
until the window frees rather than burning the quota into a hard rate-limit.

Covers:
* ``estimate_tokens`` — conservative pre-send estimate.
* ``ProviderBudget`` — RPM/RPD/TPM/TPD rolling-window enforcement, ``admits`` /
  ``blocked_until`` / ``remaining`` / ``to_dict``, unlimited passthrough, prune.
* ``BudgetLedger`` — get-or-create + record + admits.
* ``LLMClient`` integration — pre-send guard benches an exhausted provider
  (reusing the 429 cooldown path), records only SENT requests, and parses
  provider-reported token usage.

Pure stdlib; no network.
"""

from llm.client import LLMClient, _extract_usage, build_client
from llm.provider_budget import BudgetLedger, ProviderBudget, estimate_tokens


class _Clock:
    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


# ── estimate_tokens ─────────────────────────────────────────────────────────

def test_estimate_tokens_forms():
    assert estimate_tokens("", "", 0) == 0
    assert estimate_tokens("abcd", "", 0) == 1          # 4 chars → 1
    assert estimate_tokens("abcd", "efgh", 10) == 12    # 8 chars → 2, +10
    assert estimate_tokens("a" * 10, "", 0) == 3        # (10+3)//4
    assert estimate_tokens(None, None, None) == 0       # type: ignore[arg-type]


# ── ProviderBudget: unlimited passthrough ───────────────────────────────────

def test_unlimited_admits_and_meters():
    b = ProviderBudget()
    assert b.enforced is False
    for _ in range(100):
        assert b.admits(estimated_tokens=10_000, now=0.0)
        b.record(tokens=10_000, now=0.0)
    rem = b.remaining(now=0.0)
    assert rem == {"rpm": None, "rpd": None, "tpm": None, "tpd": None}


# ── ProviderBudget: per-dimension enforcement + recovery ─────────────────────

def test_rpm_enforced_and_recovers():
    b = ProviderBudget(rpm=2)
    assert b.admits(now=1000.0)
    b.record(now=1000.0)
    b.record(now=1000.5)
    assert not b.admits(now=1001.0)                      # 2 in the minute → full
    assert b.blocked_until(now=1001.0) == 59.0           # until the first ages out
    assert b.admits(now=1061.0)                          # first left the minute window


def test_rpd_enforced_and_recovers():
    b = ProviderBudget(rpd=1)
    b.record(now=0.0)
    assert not b.admits(now=100.0)
    assert b.admits(now=86401.0)                         # aged out of the day window


def test_tpm_enforced_and_recovers():
    b = ProviderBudget(tpm=100)
    b.record(tokens=80, now=1000.0)
    assert b.admits(estimated_tokens=20, now=1000.0)     # 80+20 == 100
    assert not b.admits(estimated_tokens=21, now=1000.0)  # 80+21 > 100
    assert b.blocked_until(estimated_tokens=21, now=1000.0) == 60.0
    assert b.admits(estimated_tokens=100, now=1061.0)    # 80 aged out


def test_tpd_enforced():
    b = ProviderBudget(tpd=100)
    b.record(tokens=90, now=0.0)
    assert not b.admits(estimated_tokens=20, now=100.0)
    w = b.blocked_until(estimated_tokens=20, now=100.0)
    assert 86200.0 <= w <= 86400.0
    assert b.admits(estimated_tokens=20, now=86401.0)


def test_blocked_until_zero_when_admits():
    b = ProviderBudget(rpm=5)
    assert b.blocked_until(now=0.0) == 0.0


def test_token_request_alone_exceeding_cap_waits_full_window():
    b = ProviderBudget(tpm=10)
    # A single request estimated above the whole minute budget: best-effort wait
    # is the window (never hammer); the client falls back to another provider.
    assert not b.admits(estimated_tokens=50, now=0.0)
    assert b.blocked_until(estimated_tokens=50, now=0.0) == 60.0


# ── ProviderBudget: prune + observability ────────────────────────────────────

def test_prune_drops_events_older_than_a_day():
    b = ProviderBudget(rpd=5)
    for _ in range(3):
        b.record(now=0.0)
    assert b.remaining(now=0.0)["rpd"] == 2
    assert b.remaining(now=90_000.0)["rpd"] == 5         # all aged out (> 1 day)


def test_remaining_and_to_dict():
    b = ProviderBudget(rpm=10, tpm=1000)
    b.record(tokens=100, now=0.0)
    rem = b.remaining(now=0.0)
    assert rem["rpm"] == 9 and rem["tpm"] == 900
    assert rem["rpd"] is None and rem["tpd"] is None
    d = b.to_dict(now=0.0)
    assert d["enforced"] is True
    assert d["limits"]["rpm"] == 10 and d["used"]["rpm"] == 1 and d["used"]["tpm"] == 100


# ── BudgetLedger ─────────────────────────────────────────────────────────────

def test_ledger_get_or_create_and_record():
    led = BudgetLedger()
    b = led.budget("groq", rpm=5)
    assert b is led.budget("groq")                        # limits fixed at creation
    led.record("groq", tokens=10, now=0.0)
    assert led.admits("groq", now=0.0) is True
    assert "groq" in led.to_dict(now=0.0)


# ── LLMClient integration ────────────────────────────────────────────────────

def _ok_body(tokens):
    return '{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":%d}}' % tokens


class _CountingTransport:
    def __init__(self, body):
        self.body = body
        self.calls = 0

    def __call__(self, url, headers, body, timeout):
        self.calls += 1
        return (200, self.body)


def test_client_no_budget_parses_usage():
    c = LLMClient(provider="openai", model="m", api_key="k",
                  transport=lambda *a: (200, _ok_body(123)))
    assert c.complete("s", "u") == "ok"
    assert c.last_usage_tokens == 123
    assert c.budget is None


def test_client_budget_blocks_when_exhausted_then_recovers():
    clk = _Clock(1000.0)
    budget = ProviderBudget(rpm=1, clock=clk)
    tr = _CountingTransport(_ok_body(50))
    c = LLMClient(provider="openai", model="m", api_key="k", transport=tr, budget=budget)

    assert c.complete("s", "u") == "ok"                   # first call sent
    assert tr.calls == 1 and c.last_usage_tokens == 50

    assert c.complete("s", "u") is None                   # RPM exhausted this minute
    assert tr.calls == 1                                  # NOT sent — guarded pre-flight
    assert c.last_fail_signature == "budget:exhausted"
    assert c.last_retry_after_seconds == 60.0

    clk.t += 61                                           # minute frees
    assert c.complete("s", "u") == "ok"
    assert tr.calls == 2


def test_client_budget_records_only_sent_requests():
    clk = _Clock(0.0)
    budget = ProviderBudget(rpd=10, clock=clk)
    c = LLMClient(provider="openai", model="m", api_key="k",
                  transport=lambda *a: (200, _ok_body(10)), budget=budget)
    c.complete("s", "u")
    assert budget.remaining(now=0.0)["rpd"] == 9


# ── _extract_usage per provider shape ─────────────────────────────────────────

def test_extract_usage_shapes():
    assert _extract_usage('{"usage":{"total_tokens":42}}', "openai") == 42
    assert _extract_usage('{"usage":{"prompt_tokens":10,"completion_tokens":5}}', "openai") == 15
    assert _extract_usage('{"usage":{"input_tokens":7,"output_tokens":8}}', "anthropic") == 15
    assert _extract_usage('{"usageMetadata":{"totalTokenCount":99}}', "gemini") == 99
    assert _extract_usage('{"prompt_eval_count":3,"eval_count":4}', "ollama") == 7
    assert _extract_usage("not json", "openai") == 0
    assert _extract_usage("{}", "openai") == 0


# ── build_client attaches a budget from config limits ─────────────────────────

class _Cfg:
    provider = "openai"
    model = "m"
    api_key = "k"
    base_url = ""
    timeout_seconds = 20.0
    max_tokens = 100
    temperature = 0.2
    rpm_limit = 0
    rpd_limit = 0
    tpm_limit = 0
    tpd_limit = 0


def test_build_client_attaches_budget_when_limited():
    cfg = _Cfg()
    cfg.rpm_limit = 5
    c = build_client(cfg)
    assert c is not None and c.budget is not None
    assert c.budget.rpm == 5


def test_build_client_no_budget_when_unlimited():
    c = build_client(_Cfg())
    assert c is not None and c.budget is None

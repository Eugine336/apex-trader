"""Shared per-account quota budgets across the multi-provider rosters (§22–§28).

A free-tier quota is per provider ACCOUNT, not per model — so several models on
one key, and every subsystem that calls that key (the Brain's single reasoner,
its failover roster, and the council), must share ONE meter. These tests drive
the real builders (only the completion transport is faked) and assert that:

* ``account_key`` shares same provider+endpoint+key and separates distinct keys.
* ``coerce_limits`` reads both config-attr and roster-spec dict forms.
* ``get_shared_ledger`` is a resettable process singleton.
* ``build_model_manager`` / ``build_reasoning_orchestrator`` attach ONE shared
  budget per account (two models on one key share it; a different provider with
  its own limits gets its own), with same-provider limit inheritance.
* ``build_client`` and the roster builders share the SAME budget for one account
  when given the same ledger.

Pure stdlib; no network.
"""

from llm.client import build_client
from llm.model_manager import build_model_manager
from llm.provider_budget import (
    BudgetLedger,
    account_key,
    coerce_limits,
    get_shared_ledger,
    reset_shared_ledger,
)
from llm.reasoning_orchestrator import build_reasoning_orchestrator


class _Cfg:
    """Minimal LLMConfig-like object for the builders."""

    def __init__(self, **kw):
        self.provider = kw.get("provider", "openai")
        self.model = kw.get("model", "m1")
        self.api_key = kw.get("api_key", "k")
        self.base_url = kw.get("base_url", "")
        self.timeout_seconds = 20.0
        self.max_tokens = 100
        self.temperature = 0.2
        self.min_interval_seconds = 30.0
        self.drive_decisions = False
        self.local_max_concurrency = 0
        self.circuit_failure_threshold = 3
        self.circuit_cooldown_seconds = 30.0
        self.circuit_cooldown_max_seconds = 300.0
        self.model_policy = "priority"
        self.consult_mode = "adaptive"
        self.rpm_limit = kw.get("rpm_limit", 0)
        self.rpd_limit = kw.get("rpd_limit", 0)
        self.tpm_limit = kw.get("tpm_limit", 0)
        self.tpd_limit = kw.get("tpd_limit", 0)
        self.extra_models = kw.get("extra_models", [])


_GROQ_BASE = "https://api.groq.com/openai/v1"


# ── account_key / coerce_limits ──────────────────────────────────────────────

def test_account_key_shares_and_separates():
    assert account_key("openai", "", "k") == account_key("OpenAI", "", "k")
    assert account_key("openai", "https://x/", "k") == account_key("openai", "https://x", "k")
    assert account_key("openai", "", "k1") != account_key("openai", "", "k2")
    assert account_key("openai", "", "k") != account_key("groq", "", "k")
    # keyless providers still share by provider+endpoint
    assert account_key("ollama", "http://localhost:11434/") == account_key("ollama", "http://localhost:11434")


def test_coerce_limits_dict_and_object():
    assert coerce_limits({"rpm": 5, "tpd": 100}) == (5, 0, 0, 100)
    assert coerce_limits({"rpm_limit": 9}) == (9, 0, 0, 0)
    assert coerce_limits(_Cfg(rpm_limit=3)) == (3, 0, 0, 0)
    assert coerce_limits({"rpm": "bad", "rpd": -1}) == (0, 0, 0, 0)
    assert coerce_limits({}) == (0, 0, 0, 0)


def test_shared_ledger_singleton_and_reset():
    reset_shared_ledger()
    a = get_shared_ledger()
    assert a is get_shared_ledger()
    reset_shared_ledger()
    assert get_shared_ledger() is not a


# ── build_model_manager ──────────────────────────────────────────────────────

def test_model_manager_shares_budget_per_account():
    led = BudgetLedger()
    cfg = _Cfg(rpm_limit=5, extra_models=[
        {"provider": "openai", "model": "m2"},                       # same account, inherits 5
        {"provider": "groq", "model": "gm", "api_key": "gk",
         "base_url": _GROQ_BASE, "rpm": 10},                          # own account + limits
    ])
    mgr = build_model_manager(cfg, budget_ledger=led)
    assert mgr is not None

    d = led.to_dict()
    assert len(d) == 2                                                # openai (shared) + groq
    openai_key = next(k for k in d if k.startswith("openai|"))
    groq_key = next(k for k in d if k.startswith("groq|"))
    assert d[openai_key]["limits"]["rpm"] == 5
    assert d[groq_key]["limits"]["rpm"] == 10

    clients = [c.client for c in mgr._candidates]
    openai_clients = [c for c in clients if c.provider == "openai"]
    groq_clients = [c for c in clients if c.provider == "groq"]
    assert len(openai_clients) == 2 and len(groq_clients) == 1
    assert openai_clients[0].budget is openai_clients[1].budget       # ONE shared meter
    assert groq_clients[0].budget is not openai_clients[0].budget


def test_model_manager_unmetered_by_default():
    led = BudgetLedger()
    mgr = build_model_manager(_Cfg(), budget_ledger=led)
    assert mgr is not None
    assert led.to_dict() == {}                                       # nothing metered
    assert all(c.client.budget is None for c in mgr._candidates)


# ── build_reasoning_orchestrator ─────────────────────────────────────────────

def test_orchestrator_shares_budget_per_account():
    led = BudgetLedger()
    cfg = _Cfg(rpm_limit=5, extra_models=[
        {"provider": "openai", "model": "m2"},
        {"provider": "groq", "model": "gm", "api_key": "gk",
         "base_url": _GROQ_BASE, "rpm": 10},
    ])
    orch = build_reasoning_orchestrator(cfg, budget_ledger=led)
    assert orch is not None

    d = led.to_dict()
    assert len(d) == 2
    clients = [e._reasoner.client for e in orch._engines]
    openai_clients = [c for c in clients if c.provider == "openai"]
    groq_clients = [c for c in clients if c.provider == "groq"]
    assert len(openai_clients) == 2 and len(groq_clients) == 1
    assert openai_clients[0].budget is openai_clients[1].budget
    assert groq_clients[0].budget is not openai_clients[0].budget


# ── cross-builder sharing ─────────────────────────────────────────────────────

def test_build_client_and_manager_share_one_account_budget():
    led = BudgetLedger()
    cfg = _Cfg(rpm_limit=7)
    client = build_client(cfg, budget_ledger=led)
    mgr = build_model_manager(cfg, budget_ledger=led)
    assert client is not None and mgr is not None
    primary = next(c.client for c in mgr._candidates if c.client.provider == "openai")
    assert client.budget is primary.budget                           # same meter, one account
    assert client.budget.rpm == 7

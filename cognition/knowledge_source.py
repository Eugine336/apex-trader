"""APEX TRADER — Composio knowledge source (Constitution Part IX v3.0).

The Operational Intelligence Layer's *read* half. It turns external knowledge
reached through Composio — market context, institutional research, and AI
advisors — into advisory :class:`~cognition.contracts.Evidence` for the Brain.

Design guarantees:

* **Read-only.** This never performs a side-effecting action; those still flow
  through the governed :class:`~action.orchestrator.ActionOrchestrator`. Here we
  only *retrieve* and hand the result to the Brain as evidence.
* **Advisory, never authoritative** (Article 8). Every advisor answer and every
  research passage is Evidence — never a vote, never a decision.
* **Gated + throttled + cost-aware.** Off unless explicitly enabled; retrieval
  is periodic per symbol (research is continuous, not per-tick) to bound cost.
* **Measured** (Article 12). Every call updates value metrics so the layer's
  contribution — hit rate, evidence produced, latency, faults — is observable.
* **Fail-safe.** A fault or an untrusted external payload can never raise into
  the cognition loop; it degrades to "no evidence".

Pure standard library at import time (``action`` types are passed in, not
imported) so the module stays natively importable and testable offline.
"""

from __future__ import annotations

import logging
import time as _time
from typing import Any, Callable, Optional

from cognition.evidence_adapters import evidence_from_knowledge

logger = logging.getLogger("apex.cognition.knowledge")


# ── Per-symbol query + per-tool argument helpers ─────────────────────────────

# Human/market descriptors so a web/news search is relevant to the instrument
# rather than an opaque broker code. Covers metals, energy, crypto and index
# CFDs; FX pairs and anything unlisted fall back to a generated description.
_SYMBOL_QUERY: dict = {
    "XAUUSD": "gold (XAU/USD)", "XAGUSD": "silver (XAG/USD)",
    "XBRUSD": "brent crude oil", "XTIUSD": "WTI crude oil",
    "BTCUSD": "bitcoin (BTC)", "ETHUSD": "ethereum (ETH)",
    "US500": "S&P 500 index", "US30": "Dow Jones index",
    "NAS100": "Nasdaq 100 index", "GER40": "DAX 40 index",
    "UK100": "FTSE 100 index", "JP225": "Nikkei 225 index",
    "HK50": "Hang Seng index", "AUS200": "ASX 200 index",
    "US2000": "Russell 2000 index",
}

_FX_NAMES: dict = {
    "USD": "US dollar", "EUR": "euro", "GBP": "British pound",
    "JPY": "Japanese yen", "AUD": "Australian dollar", "NZD": "New Zealand dollar",
    "CAD": "Canadian dollar", "CHF": "Swiss franc", "CNH": "Chinese yuan",
}


def symbol_query(symbol: str) -> str:
    """Turn a broker symbol into a market-relevant news/research query.

    ``XAUUSD`` → "latest market news, macro drivers and sentiment for gold
    (XAU/USD)"; ``EURUSD`` → "... for euro vs US dollar (EURUSD)". Pure.
    """
    s = str(symbol or "").upper().strip()
    if not s:
        return "latest market news and macro drivers"
    if s in _SYMBOL_QUERY:
        subject = _SYMBOL_QUERY[s]
    elif len(s) == 6 and s.isalpha() and s[:3] in _FX_NAMES and s[3:] in _FX_NAMES:
        subject = f"{_FX_NAMES[s[:3]]} vs {_FX_NAMES[s[3:]]} ({s})"
    else:
        subject = s
    return f"latest market news, macro drivers and sentiment for {subject}"


def _av_ticker(symbol: str) -> str:
    """Map a broker symbol to an Alpha Vantage NEWS_SENTIMENT ticker token.

    Alpha Vantage expects ``FOREX:EUR`` / ``CRYPTO:BTC`` / a stock symbol — not a
    raw pair. FX majors → ``FOREX:<base>``; crypto → ``CRYPTO:<base>``; metals →
    ``FOREX:<metal>``; anything else falls back to the raw symbol.
    """
    s = str(symbol or "").upper().strip()
    if len(s) == 6 and s.isalpha():
        base, quote = s[:3], s[3:]
        if base in ("BTC", "ETH", "XRP", "LTC", "BCH", "SOL", "DOGE", "ADA"):
            return f"CRYPTO:{base}"
        if base in ("XAU", "XAG", "XBR", "XTI"):
            return f"FOREX:{base}"
        if base in _FX_NAMES and quote in _FX_NAMES:
            return f"FOREX:{base}"
    return s


def build_tool_args(
    action: str, symbol: str, query: str, overrides: Optional[dict] = None,
) -> dict:
    """Build tool-appropriate arguments for a Composio action.

    Web search and generic advisors take ``{"query": ...}`` — the safe default
    used for anything not explicitly mapped, so retrieval always works. An
    ``overrides`` map (action-slug → template dict) lets the operator supply the
    exact argument schema for a connected app's tool without a code change; the
    tokens ``{query}``, ``{symbol}``, ``{ticker}`` and ``{av_ticker}`` (Alpha
    Vantage ``FOREX:``/``CRYPTO:`` form) are substituted. Pure.
    """
    act = str(action or "").upper()
    sym = str(symbol or "").upper()
    ticker = sym[:3] if (len(sym) == 6 and sym.isalpha()) else sym
    av_ticker = _av_ticker(sym)
    if overrides:
        tmpl = overrides.get(action) or overrides.get(act)
        if isinstance(tmpl, dict):
            def _sub(v: Any) -> Any:
                if isinstance(v, str):
                    return (v.replace("{query}", query)
                            .replace("{symbol}", sym)
                            .replace("{av_ticker}", av_ticker)
                            .replace("{ticker}", ticker))
                return v
            return {k: _sub(v) for k, v in tmpl.items()}
    return {"query": query}


class KnowledgeSource:
    """Composio-backed data/research/advisor layer → advisory Evidence."""

    def __init__(
        self,
        adapter: Any,
        registry: Any = None,
        *,
        enabled: bool = False,
        interval_seconds: float = 300.0,
        max_items: int = 5,
        advisor_enabled: bool = False,
        knowledge_capability: str = "knowledge.retrieve",
        advisor_capability: str = "advisor.consult",
        query_builder: Optional[Callable[[str], str]] = None,
        available_providers: Optional[list] = None,
        knowledge_provider: str = "",
        advisor_provider: str = "",
        arg_overrides: Optional[dict] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._adapter = adapter
        self._registry = registry
        self._enabled = bool(enabled)
        self._interval = max(0.0, float(interval_seconds))
        self._max_items = max(1, int(max_items))
        self._advisor_enabled = bool(advisor_enabled)
        self._knowledge_cap = str(knowledge_capability or "knowledge.retrieve")
        self._advisor_cap = str(advisor_capability or "advisor.consult")
        self._query_builder = query_builder
        # Part IX Art 11 — provider selection: which connected apps this entity
        # may use, and the preferred provider per knowledge/advisor capability
        # (e.g. knowledge.retrieve → alphavantage). Blank ⇒ the capability's
        # first candidate (web search) is used.
        self._available = {str(p).strip().lower() for p in (available_providers or []) if str(p).strip()}
        self._knowledge_provider = str(knowledge_provider or "").strip().lower()
        self._advisor_provider = str(advisor_provider or "").strip().lower()
        self._arg_overrides = dict(arg_overrides or {})
        self._clock = clock or _time.monotonic
        self._last_at: dict = {}
        # ── value metrics (Article 12) ────────────────────────────────────
        self._queries = 0
        self._hits = 0
        self._faults = 0
        self._evidence_produced = 0
        self._throttled = 0
        self._last_latency_ms = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled and self._adapter is not None and self._usable()

    def _usable(self) -> bool:
        try:
            u = getattr(self._adapter, "usable", True)
            return bool(u() if callable(u) else u)
        except Exception:  # noqa: BLE001
            return True

    def _resolve_action(self, capability: str, preferred: str = "") -> str:
        """Turn a semantic capability into its provider action string.

        Uses the capability registry when wired (so the Brain/loop reason in
        capabilities while the adapter receives the concrete Composio action),
        honouring the operator's preferred provider + the connected-provider
        availability set. Falls back to the capability name when no registry is
        available.
        """
        try:
            if self._registry is None:
                return capability
            cap = self._registry.get(capability)
            if cap is None:
                return capability
            binding = self._registry.resolve_provider(
                cap, preferred=preferred,
                available=(self._available or None),
            )
            return getattr(binding, "action", "") or capability
        except Exception:  # noqa: BLE001
            return capability

    def _default_query(self, symbol: str) -> str:
        return symbol_query(symbol)

    def evidence_for(self, symbol: str, *, now: Optional[float] = None) -> list:
        """Return advisory Evidence for ``symbol`` (``[]`` when off/throttled)."""
        if not self.enabled:
            return []
        sym = str(symbol or "")
        if not sym:
            return []
        t = self._clock() if now is None else float(now)
        # Per-symbol throttle — research is periodic, not per-cycle (cost-aware).
        last = self._last_at.get(sym, 0.0)
        if self._interval > 0.0 and (t - last) < self._interval:
            self._throttled += 1
            return []
        self._last_at[sym] = t
        query = (
            self._query_builder(sym) if self._query_builder is not None
            else self._default_query(sym)
        )
        out: list = []
        out.extend(self._consult(
            self._knowledge_cap, sym, query, "composio.knowledge",
            preferred=self._knowledge_provider,
        ))
        if self._advisor_enabled:
            out.extend(self._consult(
                self._advisor_cap, sym, query, "composio.advisor",
                preferred=self._advisor_provider,
            ))
        self._evidence_produced += len(out)
        return out

    def _consult(self, capability: str, symbol: str, query: str, source: str,
                 *, preferred: str = "") -> list:
        started = self._clock()
        try:
            action = self._resolve_action(capability, preferred=preferred)
            self._queries += 1
            args = build_tool_args(action, symbol, query, self._arg_overrides)
            result = self._adapter.execute(action, args)
            payload = getattr(result, "data", None) or {}
            if not bool(getattr(result, "ok", False)) or not payload:
                return []
            self._hits += 1
            return evidence_from_knowledge(
                symbol, payload, source=source, max_items=self._max_items,
            )
        except Exception as exc:  # noqa: BLE001 — never break the cognition loop
            self._faults += 1
            logger.debug("[knowledge] consult(%s) fault for %s: %s", capability, symbol, exc)
            return []
        finally:
            self._last_latency_ms = round((self._clock() - started) * 1000.0, 2)

    def get_status(self) -> dict:
        """Value metrics (Article 12) — how much this layer is contributing."""
        return {
            "enabled": self.enabled,
            "queries": self._queries,
            "hits": self._hits,
            "faults": self._faults,
            "throttled": self._throttled,
            "evidence_produced": self._evidence_produced,
            "hit_rate": round(self._hits / self._queries, 4) if self._queries else 0.0,
            "last_latency_ms": self._last_latency_ms,
            "interval_seconds": self._interval,
            "advisor_enabled": self._advisor_enabled,
        }


__all__ = ["KnowledgeSource", "symbol_query", "build_tool_args"]

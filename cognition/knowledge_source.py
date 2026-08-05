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

    def _resolve_action(self, capability: str) -> str:
        """Turn a semantic capability into its provider action string.

        Uses the capability registry when wired (so the Brain/loop reason in
        capabilities while the adapter receives the concrete Composio action);
        falls back to the capability name when no registry is available.
        """
        try:
            if self._registry is None:
                return capability
            cap = self._registry.get(capability)
            if cap is None:
                return capability
            binding = self._registry.resolve_provider(cap)
            return getattr(binding, "action", "") or capability
        except Exception:  # noqa: BLE001
            return capability

    def _default_query(self, symbol: str) -> str:
        return f"latest market news, macro drivers and sentiment for {symbol}"

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
        out.extend(self._consult(self._knowledge_cap, sym, query, "composio.knowledge"))
        if self._advisor_enabled:
            out.extend(self._consult(self._advisor_cap, sym, query, "composio.advisor"))
        self._evidence_produced += len(out)
        return out

    def _consult(self, capability: str, symbol: str, query: str, source: str) -> list:
        started = self._clock()
        try:
            action = self._resolve_action(capability)
            self._queries += 1
            result = self._adapter.execute(action, {"query": query})
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


__all__ = ["KnowledgeSource"]

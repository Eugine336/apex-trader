"""APEX TRADER — Reasoning Orchestrator (Constitution Part XVII).

Part XVII preserves the Single Reasoner Principle while letting the one Cognitive
Brain *consult* several reasoning engines. This module is the orchestrator that
sits between the Brain and the external engines (Article 9): it selects engines
by capability + measured reliability, fans a query out to them, collects each
engine's structured opinion, and returns them all to the Brain — as *advisory
evidence*, never a vote (Article 7). The orchestrator has **no market authority**
and never synthesises a decision: it does not average, rank-to-winner, or pick a
majority. The Brain evaluates and challenges every opinion and constructs the
final thesis (Articles 6, 10, 12).

An "engine" is any duck-typed reasoner exposing ``available`` and
``reason(symbol, evidence, now=None) -> opinion`` (the shape of
:class:`llm.reasoner.LLMReasoner`). The orchestrator records per-engine health
(calls, faults, EWMA latency) for continuous validation (Article 11); an optional
``reliability_provider`` lets an external learner (the Phase J influence ledger)
inform selection order by *measured* usefulness rather than assumption.

Pure standard library; fail-safe throughout — a consultation fault yields fewer
opinions, never an exception.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from loguru import logger


@dataclass
class EngineOpinion:
    """One engine's opinion within a consultation (engine name + the opinion)."""

    engine: str
    direction: str
    confidence: float
    rationale: str = ""
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "direction": self.direction,
            "confidence": round(self.confidence, 4),
            "rationale": self.rationale[:200],
            "latency_ms": round(self.latency_ms, 1),
        }


@dataclass
class ReasoningConsultation:
    """The set of engine opinions for one query — advisory, never a decision.

    Deliberately carries NO aggregate direction/confidence: synthesis is the
    Brain's job (Article 7/12). ``opinions`` is simply every engine that replied.
    """

    symbol: str
    opinions: list = field(default_factory=list)      # list[EngineOpinion]
    consulted: list = field(default_factory=list)     # engine names asked
    capability: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "capability": self.capability,
            "consulted": list(self.consulted),
            "opinions": [o.to_dict() for o in self.opinions],
        }


class ReasoningEngine:
    """Wraps one reasoner with a name, capability tags and live health stats."""

    def __init__(self, name: str, reasoner: Any, *, capabilities: Optional[list] = None) -> None:
        self.name = str(name or "engine")
        self._reasoner = reasoner
        self.capabilities = tuple(str(c).strip().lower() for c in (capabilities or []) if str(c).strip())
        self.calls = 0
        self.faults = 0
        self.ewma_latency_ms = 0.0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        try:
            return bool(getattr(self._reasoner, "available", False))
        except Exception:  # noqa: BLE001
            return False

    def has_capability(self, capability: str) -> bool:
        cap = str(capability or "").strip().lower()
        return (not cap) or (cap in self.capabilities)

    @property
    def reliability(self) -> float:
        with self._lock:
            total = self.calls + self.faults
            return 1.0 if total == 0 else self.calls / total

    def consult(self, symbol: str, evidence: dict, *, now: Optional[float] = None) -> Optional[EngineOpinion]:
        """Ask this engine for an opinion. Times + records health. Fail-safe."""
        if not self.available:
            return None
        t0 = time.time()
        try:
            op = self._reasoner.reason(symbol, evidence, now=now)
        except Exception as exc:  # noqa: BLE001 — an engine fault must not break consultation
            logger.debug("[reasoning-orch] engine {} raised: {}", self.name, exc)
            op = None
        latency_ms = (time.time() - t0) * 1000.0
        with self._lock:
            a = 0.3
            self.ewma_latency_ms = (latency_ms if self.ewma_latency_ms <= 0.0
                                    else (1 - a) * self.ewma_latency_ms + a * latency_ms)
            if op is None:
                self.faults += 1
                return None
            self.calls += 1
        direction = str(getattr(op, "direction", "FLAT") or "FLAT").upper()
        try:
            confidence = float(getattr(op, "confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return EngineOpinion(
            engine=self.name, direction=direction,
            confidence=min(1.0, max(0.0, confidence)),
            rationale=str(getattr(op, "rationale", "") or ""),
            latency_ms=latency_ms,
        )

    def to_dict(self) -> dict:
        # Compute reliability inline under the lock — calling self.reliability
        # here would re-acquire the same (non-reentrant) lock and deadlock.
        with self._lock:
            calls, faults, lat = self.calls, self.faults, self.ewma_latency_ms
        total = calls + faults
        reliability = 1.0 if total == 0 else calls / total
        return {
            "name": self.name,
            "capabilities": list(self.capabilities),
            "available": self.available,
            "calls": calls,
            "faults": faults,
            "reliability": round(reliability, 4),
            "ewma_latency_ms": round(lat, 1),
        }


class ReasoningOrchestrator:
    """Selects + consults several reasoning engines; returns all opinions.

    Never votes, averages, or decides (Article 7). ``consult`` returns a
    :class:`ReasoningConsultation` the Brain evaluates. Fail-safe.
    """

    def __init__(
        self,
        engines: list,
        *,
        max_engines: int = 3,
        reliability_provider: Optional[Callable[[str], float]] = None,
    ) -> None:
        self._engines = [e for e in (engines or []) if isinstance(e, ReasoningEngine)]
        self.max_engines = max(1, int(max_engines))
        # Optional externally-measured usefulness (e.g. Phase J influence ledger
        # weight for source_module "reasoning_engine.<name>") — Article 11.
        self._reliability_provider = reliability_provider
        self._consultations = 0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return any(e.available for e in self._engines)

    def _measured_usefulness(self, engine: ReasoningEngine) -> float:
        if self._reliability_provider is None:
            return engine.reliability
        try:
            return float(self._reliability_provider(f"reasoning_engine.{engine.name}"))
        except Exception:  # noqa: BLE001
            return engine.reliability

    def select(self, *, capability: str = "", max_engines: Optional[int] = None) -> list:
        """Available engines for a capability, best-first (Article 8/11).

        Ordered by measured usefulness (desc) then lower latency; capped. When a
        capability is requested but no engine declares it, falls back to all
        available engines (better a generalist opinion than none).
        """
        avail = [e for e in self._engines if e.available]
        matching = [e for e in avail if e.has_capability(capability)] if capability else avail
        pool = matching or avail
        pool = sorted(pool, key=lambda e: (-self._measured_usefulness(e), e.ewma_latency_ms))
        cap = self.max_engines if max_engines is None else max(1, int(max_engines))
        return pool[:cap]

    def consult(
        self,
        symbol: str,
        evidence: dict,
        *,
        now: Optional[float] = None,
        capability: str = "",
        max_engines: Optional[int] = None,
    ) -> ReasoningConsultation:
        """Fan out to the selected engines and collect every opinion. No vote."""
        result = ReasoningConsultation(symbol=str(symbol or ""), capability=str(capability or ""))
        try:
            selected = self.select(capability=capability, max_engines=max_engines)
            result.consulted = [e.name for e in selected]
            for engine in selected:
                op = engine.consult(symbol, evidence, now=now)
                if op is not None:
                    result.opinions.append(op)
            with self._lock:
                self._consultations += 1
        except Exception as exc:  # noqa: BLE001 — consultation must never raise
            logger.debug("[reasoning-orch] consult({}) fault: {}", symbol, exc)
        return result

    def get_status(self) -> dict:
        with self._lock:
            consultations = self._consultations
        return {
            "available": self.available,
            "engine_count": len(self._engines),
            "available_engines": sum(1 for e in self._engines if e.available),
            "max_engines": self.max_engines,
            "consultations": consultations,
            "engines": [e.to_dict() for e in self._engines],
        }


def build_reasoning_orchestrator(
    config: Any,
    *,
    transport: Optional[Any] = None,
    reliability_provider: Optional[Callable[[str], float]] = None,
) -> Optional[ReasoningOrchestrator]:
    """Build a :class:`ReasoningOrchestrator` from an ``LLMConfig``-like object.

    Each candidate model (the primary plus every ``extra_models`` entry) becomes
    a named :class:`ReasoningEngine` backed by its own
    :class:`llm.reasoner.LLMReasoner`. Engines inherit the primary key/base_url
    when a spec omits them (the "one gateway, many models" case). Returns ``None``
    when fewer than one usable engine can be built. Never raises.
    """
    try:
        from llm.client import LLMClient
        from llm.reasoner import LLMReasoner
    except Exception as exc:  # noqa: BLE001
        logger.debug("[reasoning-orch] build import failed: {}", exc)
        return None

    primary_key = str(getattr(config, "api_key", "") or "")
    primary_base = str(getattr(config, "base_url", "") or "")
    to = float(getattr(config, "timeout_seconds", 20.0) or 20.0)
    mt = int(getattr(config, "max_tokens", 512) or 512)
    tmp = float(getattr(config, "temperature", 0.2) or 0.2)
    interval = float(getattr(config, "min_interval_seconds", 30.0) or 30.0)
    drive = bool(getattr(config, "drive_decisions", False))

    def _engine(provider, model, api_key, base_url, caps, name):
        if not provider or not model:
            return None
        client = LLMClient(
            provider=str(provider), model=str(model),
            api_key=str(api_key or ""), base_url=str(base_url or ""),
            timeout_seconds=to, max_tokens=mt, temperature=tmp, transport=transport,
        )
        if not client.usable:
            return None
        reasoner = LLMReasoner(client=client, enabled=True, drive_decisions=drive,
                               min_interval_seconds=interval)
        return ReasoningEngine(name, reasoner, capabilities=caps)

    engines: list = []
    seen_names: set = set()

    def _add(eng):
        if eng is not None and eng.name not in seen_names:
            engines.append(eng)
            seen_names.add(eng.name)

    primary_provider = getattr(config, "provider", "")
    primary_model = getattr(config, "model", "")
    _add(_engine(primary_provider, primary_model, primary_key, primary_base,
                 [], str(primary_model or primary_provider or "primary")))

    specs = getattr(config, "extra_models", None)
    if isinstance(specs, list):
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name") or spec.get("model") or spec.get("provider") or "engine")
            _add(_engine(
                spec.get("provider", primary_provider), spec.get("model", ""),
                spec.get("api_key", primary_key), spec.get("base_url", primary_base),
                spec.get("capabilities", []), name,
            ))

    if not engines:
        return None
    return ReasoningOrchestrator(
        engines,
        max_engines=int(getattr(config, "consult_max_engines", 3) or 3),
        reliability_provider=reliability_provider,
    )


__all__ = [
    "EngineOpinion",
    "ReasoningConsultation",
    "ReasoningEngine",
    "ReasoningOrchestrator",
    "build_reasoning_orchestrator",
]

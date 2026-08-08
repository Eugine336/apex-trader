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

from llm.health import CircuitBreaker, CircuitConfig, ConcurrencyLimiter
from llm.provider_tiers import ProviderTier, resolve_tier


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

    def __init__(self, name: str, reasoner: Any, *, capabilities: Optional[list] = None,
                 circuit: Optional[CircuitConfig] = None, is_local: bool = False,
                 local_limiter: Optional[ConcurrencyLimiter] = None,
                 local_acquire_timeout: float = 30.0) -> None:
        self.name = str(name or "engine")
        self._reasoner = reasoner
        self.capabilities = tuple(str(c).strip().lower() for c in (capabilities or []) if str(c).strip())
        self.calls = 0
        self.faults = 0
        self.ewma_latency_ms = 0.0
        self.is_local = bool(is_local)
        self.breaker = CircuitBreaker(circuit)
        self._local_limiter = local_limiter
        self._local_acquire_timeout = max(0.0, float(local_acquire_timeout))
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        """Usable reasoner AND the circuit admits (healthy / half-open probe).

        A circuit tripped OPEN by repeated real failures removes this advisor
        from the panel for its cooldown so a dead advisor is not re-consulted
        every cycle; it rejoins automatically on recovery."""
        try:
            base = bool(getattr(self._reasoner, "available", False))
        except Exception:  # noqa: BLE001
            base = False
        return base and self.breaker.admits()

    def has_capability(self, capability: str) -> bool:
        cap = str(capability or "").strip().lower()
        return (not cap) or (cap in self.capabilities)

    @property
    def reliability(self) -> float:
        with self._lock:
            total = self.calls + self.faults
            return 1.0 if total == 0 else self.calls / total

    def consult(self, symbol: str, evidence: dict, *, now: Optional[float] = None) -> Optional[EngineOpinion]:
        """Ask this engine for an opinion. Health-tracked + circuit-broken.

        A LOCAL (CPU/GPU-bound) engine first acquires a shared concurrency slot
        so a full panel does not start every local model at once and exhaust the
        host; if no slot frees within the acquire timeout it simply skips this
        cycle (absent) rather than piling onto an overloaded host. Fail-safe."""
        if not self.available:
            return None
        if self.is_local and self._local_limiter is not None:
            with self._local_limiter.slot(timeout=self._local_acquire_timeout) as got:
                if not got:
                    logger.debug("[reasoning-orch] {} skipped — no local compute slot free", self.name)
                    return None
                return self._consult_inner(symbol, evidence, now=now)
        return self._consult_inner(symbol, evidence, now=now)

    def _consult_inner(self, symbol: str, evidence: dict, *, now: Optional[float] = None) -> Optional[EngineOpinion]:
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
            else:
                self.calls += 1
        if op is None:
            # Trip the circuit only on a REAL failure (provider down / timeout /
            # unparsable) — never on a benign throttle/no-op — using the
            # reasoner's liveness signal, so a rate-limited advisor is not wrongly
            # circuit-broken out of the panel.
            if self._reason_degraded(symbol):
                self.breaker.record_failure(latency_ms)
            return None
        self.breaker.record_success(latency_ms)
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

    def _reason_degraded(self, symbol: str) -> bool:
        """True when the reasoner reports its last call really failed (not a
        throttle). Defaults to True when the reasoner exposes no signal, so a
        plain None still counts toward the circuit after the threshold."""
        fn = getattr(self._reasoner, "last_reason_degraded", None)
        if not callable(fn):
            return True
        try:
            return bool(fn(symbol))
        except Exception:  # noqa: BLE001
            return True

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
            "is_local": self.is_local,
            "calls": calls,
            "faults": faults,
            "reliability": round(reliability, 4),
            "ewma_latency_ms": round(lat, 1),
            "circuit": self.breaker.to_dict(),
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
        panel: bool = False,
    ) -> None:
        self._engines = [e for e in (engines or []) if isinstance(e, ReasoningEngine)]
        self.max_engines = max(1, int(max_engines))
        # Part XXIV — panel mode: when true, the council consults EVERY available
        # advisor in parallel (a full panel), not a capped subset. A dead advisor
        # simply yields no opinion and leaves the panel; the rest still advise —
        # never a failover chain.
        self.panel = bool(panel)
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
        if max_engines is None:
            cap = self.max_engines
        elif int(max_engines) <= 0:
            cap = len(pool)          # panel: EVERY available advisor advises
        else:
            cap = max(1, int(max_engines))
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
            # Part XXIV — fan out to EVERY selected advisor CONCURRENTLY. Each
            # consult is a blocking provider round-trip (or, for a local model,
            # a CPU-bound generation wait), so consulting serially made a full
            # panel cost the SUM of every advisor's latency/timeout — minutes —
            # and forced slow local models to share one tiny timeout. Threads let
            # a slow advisor (e.g. an 8B model on CPU) run to completion in
            # parallel with the rest; the panel now takes ~the slowest advisor,
            # not the sum. consult() is fully fail-safe and each engine owns its
            # own health/state lock, so one thread's fault never touches another.
            if len(selected) <= 1:
                for engine in selected:
                    op = engine.consult(symbol, evidence, now=now)
                    if op is not None:
                        result.opinions.append(op)
            else:
                import concurrent.futures as _futures

                with _futures.ThreadPoolExecutor(
                    max_workers=len(selected),
                    thread_name_prefix="council",
                ) as pool:
                    pending = [
                        pool.submit(engine.consult, symbol, evidence, now=now)
                        for engine in selected
                    ]
                    for fut in _futures.as_completed(pending):
                        try:
                            op = fut.result()
                        except Exception:  # noqa: BLE001 — per-advisor isolation
                            op = None
                        if op is not None:
                            result.opinions.append(op)
            with self._lock:
                self._consultations += 1
            # Part XXIV — surface the live panel: who advised (with their vote)
            # and who was asked but did not reply (an advisor that "left the
            # panel" this cycle). One concise INFO line so the operator can see,
            # at a glance, which AIs are still in it when one fails.
            if result.consulted:
                self._log_panel(result)
        except Exception as exc:  # noqa: BLE001 — consultation must never raise
            logger.debug("[reasoning-orch] consult({}) fault: {}", symbol, exc)
        return result

    @staticmethod
    def _log_panel(result: "ReasoningConsultation") -> None:
        try:
            replied = {str(getattr(o, "engine", "") or "") for o in result.opinions}
            asked = list(result.consulted)
            absent = [n for n in asked if n not in replied]
            advising = ", ".join(
                f"{getattr(o, 'engine', '?')} {getattr(o, 'direction', '?')}"
                f"({float(getattr(o, 'confidence', 0.0) or 0.0):.2f})"
                for o in result.opinions
            )
            logger.info(
                "[council] {} — {}/{} advising: {}{}",
                result.symbol, len(result.opinions), len(asked),
                advising or "(none replied)",
                (" | absent: " + ", ".join(absent)) if absent else "",
            )
        except Exception:  # noqa: BLE001 — logging must never break consultation
            pass

    def get_status(self) -> dict:
        with self._lock:
            consultations = self._consultations
        return {
            "available": self.available,
            "engine_count": len(self._engines),
            "available_engines": sum(1 for e in self._engines if e.available),
            "max_engines": self.max_engines,
            "panel": self.panel,
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
    # §3/§11 — shared cap on concurrent LOCAL model calls (0 ⇒ unbounded) so a
    # full panel does not start every local model at once and exhaust the host.
    local_limiter = ConcurrencyLimiter(int(getattr(config, "local_max_concurrency", 1) or 0))
    # §7/§9 — shared circuit-breaker tuning for every advisor.
    circuit = CircuitConfig(
        failure_threshold=int(getattr(config, "circuit_failure_threshold", 3) or 3),
        cooldown_seconds=float(getattr(config, "circuit_cooldown_seconds", 30.0) or 30.0),
        cooldown_max_seconds=float(getattr(config, "circuit_cooldown_max_seconds", 300.0) or 300.0),
    )

    def _engine(provider, model, api_key, base_url, caps, name,
                spec_tier=None, timeout=None, max_tokens=None, temperature=None):
        if not provider or not model:
            return None
        # Per-provider credentials (Part XXIII Art 15): resolve <PROVIDER>_API_KEY
        # and inherit the primary key/base only for the same provider. A
        # credential-requiring provider with no key is skipped — a blank roster
        # entry is inert (never an advisor, never a failing call).
        from llm.provider_credentials import (
            has_credentials, resolve_api_key, resolve_base_url,
        )
        key = resolve_api_key(provider, api_key,
                              primary_provider=primary_provider, primary_key=primary_key)
        base = resolve_base_url(provider, base_url,
                               primary_provider=primary_provider, primary_base=primary_base)
        if not has_credentials(provider, key):
            return None
        eff_timeout = float(timeout if timeout is not None else to)
        client = LLMClient(
            provider=str(provider), model=str(model),
            api_key=str(key or ""), base_url=str(base or ""),
            timeout_seconds=eff_timeout,
            max_tokens=int(max_tokens if max_tokens is not None else mt),
            temperature=float(temperature if temperature is not None else tmp),
            transport=transport,
        )
        if not client.usable:
            return None
        reasoner = LLMReasoner(client=client, enabled=True, drive_decisions=drive,
                               min_interval_seconds=interval)
        # Tier 3 = local (CPU/GPU-bound) — gate it behind the shared local slot.
        is_local = int(resolve_tier(provider, spec_tier)) == int(ProviderTier.TIER_3)
        return ReasoningEngine(
            name, reasoner, capabilities=caps, circuit=circuit,
            is_local=is_local, local_limiter=local_limiter,
            local_acquire_timeout=eff_timeout,
        )

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
                spec.get("api_key", ""), spec.get("base_url", ""),
                spec.get("capabilities", []), name,
                spec_tier=spec.get("tier"),
                timeout=spec.get("timeout_seconds"), max_tokens=spec.get("max_tokens"),
                temperature=spec.get("temperature"),
            ))

    if not engines:
        return None
    panel = str(getattr(config, "consult_mode", "adaptive") or "adaptive").strip().lower() == "panel"
    logger.info(
        "[council] assembled {} advisor(s) [{}]: {}",
        len(engines), "panel" if panel else "adaptive",
        ", ".join(f"{e.name}{'' if e.available else ' (configured)'}" for e in engines),
    )
    return ReasoningOrchestrator(
        engines,
        max_engines=int(getattr(config, "consult_max_engines", 3) or 3),
        reliability_provider=reliability_provider,
        panel=panel,
    )


__all__ = [
    "EngineOpinion",
    "ReasoningConsultation",
    "ReasoningEngine",
    "ReasoningOrchestrator",
    "build_reasoning_orchestrator",
]

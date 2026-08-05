"""APEX TRADER — Cognition loop, evidence consolidation, and action bridge.

Wires the single :class:`~cognition.brain.CognitiveBrain` into the running
system without disturbing the hot path:

* :class:`EvidenceConsolidator` — turns the current subsystem readings (the
  ThesisEngine's competing-thesis status, etc.) into structured
  :class:`~cognition.contracts.Evidence` and a
  :class:`~cognition.contracts.MarketState`. This is the constitutional
  "modules become evidence providers" seed (Part III / Part V): a read-only,
  fail-safe view — it never triggers analysis or touches execution.
* :class:`CognitionLoop` — a background daemon (never the tick loop) that, per
  symbol, consolidates evidence → asks the Brain to reason → records the
  resulting DecisionPackage/CampaignSpecification. Runs in **shadow mode** by
  default: it produces and surfaces decisions for observability without driving
  execution, so the legacy path stays authoritative until the cutover is
  validated (Part XIII/XV). ``latest()`` exposes the Brain's read for the
  eventual execution bridge.
* :class:`BrainActionBridge` — turns notable Brain decisions into governed
  Composio objectives (Part IX: AI creates objectives → governance authorises →
  Composio executes). Ecosystem actions only; never broker orders.

Pure standard library at import time (``action`` is imported lazily so this
module stays natively importable/testable); fail-safe throughout.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from cognition.campaign_translator import translate as _translate
from cognition.contracts import DecisionType, MarketState
from cognition.evidence_adapters import (
    evidence_from_analogues,
    evidence_from_reasoning,
    evidence_from_thesis_status,
    evidence_from_votes,
)

logger = logging.getLogger("apex.cognition.loop")

SymbolsProvider = Callable[[], "list[str]"]


class EvidenceConsolidator:
    """Builds a consolidated :class:`MarketState` from live subsystem readings.

    Phase E: converts the full vote panel (every contributing analytical module,
    surfaced via the ThesisEngine's supporting/opposing modules) into
    domain-classified :class:`~cognition.contracts.Evidence`, plus an aggregate
    read. Read-only and fail-safe. An optional ``vote_source`` callable supplies
    the raw WorldModel vote panel for even richer evidence when available.
    """

    def __init__(
        self,
        ctx: Optional[Any] = None,
        *,
        vote_source: Optional[Callable[[str], Any]] = None,
        per_module: bool = True,
        memory: Optional[Any] = None,
        max_analogues: int = 5,
        influence: Optional[Any] = None,
        influence_enabled: bool = False,
        reasoning: Optional[Any] = None,
    ) -> None:
        self._ctx = ctx
        self._vote_source = vote_source
        self._per_module = bool(per_module)
        self._memory = memory
        self._max_analogues = max(1, int(max_analogues))
        self._influence = influence
        self._influence_enabled = bool(influence_enabled)
        self._reasoning = reasoning

    def build(
        self,
        symbol: str,
        *,
        injected: Optional[list] = None,
        now: Optional[float] = None,
    ) -> MarketState:
        ms = MarketState(symbol=str(symbol or ""))
        try:
            if injected:
                for e in injected:
                    ms.add(e)
            ctx = self._ctx
            engine = getattr(ctx, "thesis_engine", None) if ctx is not None else None
            if engine is not None:
                status = engine.get_status() or {}
                for e in evidence_from_thesis_status(status, ms.symbol):
                    # When per-module is off, keep only the aggregate read.
                    if self._per_module or e.source_module == "brain.thesis_engine":
                        ms.add(e)
            if self._vote_source is not None:
                try:
                    for e in evidence_from_votes(ms.symbol, self._vote_source(ms.symbol)):
                        ms.add(e)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[consolidator] vote source fault (%s): %s", symbol, exc)
            # Part XVII — consult multiple reasoning engines; each opinion becomes
            # advisory Evidence (never a vote). The Brain synthesises them.
            if self._reasoning is not None:
                try:
                    if getattr(self._reasoning, "available", False):
                        payload = {
                            "consolidation": ms.consolidation(now),
                            "evidence": [e.to_dict() for e in ms.fresh_evidence(now)[:64]],
                        }
                        consult = self._reasoning.consult(ms.symbol, payload, now=now)
                        for e in evidence_from_reasoning(ms.symbol, consult):
                            ms.add(e)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[consolidator] reasoning consult fault (%s): %s", symbol, exc)
            # Part VII — consult institutional memory: surface similar past
            # campaigns and their outcomes as a historical-analogue Evidence.
            if self._memory is not None:
                try:
                    analogues = self._memory.find_analogues(ms, limit=self._max_analogues)
                    for e in evidence_from_analogues(ms.symbol, analogues):
                        ms.add(e)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[consolidator] memory recall fault (%s): %s", symbol, exc)
            # Part VIII — attach learned per-source influence weights so the
            # Brain's consolidation weights each source by demonstrated quality.
            # Observational unless explicitly enabled (shadow → authoritative).
            if self._influence_enabled and self._influence is not None:
                try:
                    sources = {e.source_module for e in ms.evidence if e.source_module}
                    ms.influence_weights = {
                        s: self._influence.weight_for(s) for s in sources
                    }
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[consolidator] influence weighting fault (%s): %s", symbol, exc)
        except Exception as exc:  # noqa: BLE001 — consolidation must never break
            logger.debug("[consolidator] build(%s) ignored a fault: %s", symbol, exc)
        return ms


class BrainActionBridge:
    """Turns notable Brain decisions into governed Composio objectives."""

    def __init__(
        self,
        orchestrator: Optional[Any] = None,
        *,
        notify_enabled: bool = True,
        source: str = "ai_brain",
    ) -> None:
        self._orchestrator = orchestrator
        self.notify_enabled = bool(notify_enabled)
        self._source = str(source or "ai_brain")
        self._emitted = 0

    def on_decision(self, output: Any) -> None:
        """Emit an operator-notification objective when a campaign opens. Fail-safe."""
        if self._orchestrator is None or not self.notify_enabled or output is None:
            return
        try:
            decision = output.decision
            if not getattr(decision, "authorises_action", False):
                return
            from action.orchestrator import ActionObjective, RiskTier  # lazy

            obj = ActionObjective(
                capability="operator.notify",
                objective=f"Campaign opened on {decision.symbol} ({output.direction})",
                params={
                    "symbol": decision.symbol,
                    "direction": output.direction,
                    "confidence": round(decision.confidence, 4),
                    "thesis": decision.thesis[:280],
                },
                expected_outcome="operator informed of a new campaign",
                confidence=decision.confidence,
                priority=3,
                reversible=True,
                risk_tier=RiskTier.NEGLIGIBLE,
                source=self._source,
                evidence_ref=decision.decision_id,
            )
            self._orchestrator.submit(obj)
            self._emitted += 1
        except Exception as exc:  # noqa: BLE001 — the bridge must never break the loop
            logger.debug("[brain-bridge] on_decision ignored a fault: %s", exc)

    def get_status(self) -> dict:
        return {"notify_enabled": self.notify_enabled, "objectives_emitted": self._emitted}


class CognitionLoop:
    """Background daemon that drives the Brain over consolidated evidence."""

    def __init__(
        self,
        brain: Any,
        consolidator: EvidenceConsolidator,
        symbols_provider: SymbolsProvider,
        *,
        interval_seconds: float = 30.0,
        max_symbols_per_cycle: int = 12,
        shadow_mode: bool = True,
        action_bridge: Optional[BrainActionBridge] = None,
        position_source: Optional[Callable[[], Any]] = None,
        origination_mode: str = "shadow",
        origination_risk_fraction: float = 0.01,
        origination_max_exposure: float = 1.0,
        balance_provider: Optional[Callable[[str], float]] = None,
        memory: Optional[Any] = None,
        operations_author: Optional[Any] = None,
        operations_sink: Optional[Callable[[Any], None]] = None,
        name: str = "cognition-loop",
    ) -> None:
        self._brain = brain
        self._consolidator = consolidator
        self._symbols_provider = symbols_provider
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.max_symbols_per_cycle = max(1, int(max_symbols_per_cycle))
        self.shadow_mode = bool(shadow_mode)
        self._action_bridge = action_bridge
        self._position_source = position_source
        m = str(origination_mode or "shadow").strip().lower()
        self.origination_mode = m if m in ("off", "shadow", "live") else "shadow"
        self._risk_fraction = max(0.0, float(origination_risk_fraction))
        self._max_exposure = min(1.0, max(0.0, float(origination_max_exposure)))
        self._balance_provider = balance_provider
        self._memory = memory
        self._operations_author = operations_author
        self._operations_sink = operations_sink
        self._origination_sink: Optional[Callable[[Any], None]] = None
        self._name = str(name or "cognition-loop")
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cycles = 0
        self._decisions = 0
        self._managed = 0
        self._orig_intended = 0
        self._orig_submitted = 0
        self._memory_opens = 0
        self._ops_submitted = 0
        self._open_keys: set = set()

    def set_origination_sink(self, sink: Optional[Callable[[Any], None]]) -> None:
        """Wire the live order-submission sink (set by the system that owns the
        executor). When unset, ``live`` origination degrades to shadow-record."""
        self._origination_sink = sink

    @property
    def running(self) -> bool:
        return self._running

    def run_once(self, *, now: Optional[float] = None) -> int:
        """Reason once over each tracked symbol. Returns decisions produced. Fail-safe."""
        try:
            symbols = list(self._symbols_provider() or [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition-loop] symbols provider fault: %s", exc)
            return 0
        self._open_keys = self._current_open_keys()
        made = 0
        for symbol in symbols[: self.max_symbols_per_cycle]:
            try:
                ms = self._consolidator.build(symbol, now=now)
                output = self._brain.reason(ms, now=now)
                made += 1
                if self._action_bridge is not None:
                    self._action_bridge.on_decision(output)
                if self._memory is not None:
                    self._record_open_memory(output, ms, now=now)
                if self.origination_mode != "off":
                    self._maybe_originate(output, now=now)
            except Exception as exc:  # noqa: BLE001 — one symbol must not stop the loop
                logger.debug("[cognition-loop] reason(%s) fault: %s", symbol, exc)
                continue
        self._cycles += 1
        self._decisions += made
        self._manage_open_positions(now=now)
        self._maybe_author_operations(now=now)
        return made

    def _maybe_author_operations(self, *, now: Optional[float] = None) -> None:
        """Drain the operations author and submit objectives via the sink. Fail-safe.

        Part IX Article 9/11: the Brain-side author emits operational objectives
        (semantic); the wired sink is the Action Planner, which selects a provider
        and submits through governed Composio. Inert unless both are wired and the
        author is enabled.
        """
        author = self._operations_author
        sink = self._operations_sink
        if author is None or sink is None or not getattr(author, "enabled", False):
            return
        try:
            mem_status = None
            if self._memory is not None:
                try:
                    mem_status = self._memory.get_status()
                except Exception:  # noqa: BLE001
                    mem_status = None
            for intent in author.tick(now=now, memory_status=mem_status) or []:
                try:
                    sink(intent)
                    self._ops_submitted += 1
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[cognition-loop] operations sink fault: %s", exc)
        except Exception as exc:  # noqa: BLE001 — operations must never break the loop
            logger.debug("[cognition-loop] author operations fault: %s", exc)

    def _current_open_keys(self) -> set:
        """Set of ``(symbol, direction)`` for currently open positions/campaigns.

        Used to suppress duplicate origination for a book the Brain already holds.
        Fail-safe: an unreadable position source yields an empty set.
        """
        keys: set = set()
        if self._position_source is None:
            return keys
        try:
            for pos in list(self._position_source() or []):
                sym = str(getattr(pos, "symbol", "") or "")
                d = str(getattr(pos, "direction", "") or "").upper()
                if sym and d in ("LONG", "SHORT"):
                    keys.add((sym, d))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition-loop] open-keys read fault: %s", exc)
        return keys

    def _record_open_memory(self, output: Any, market_state: Any, *,
                            now: Optional[float] = None) -> None:
        """Persist the OPEN-time state fingerprint + spec to memory. Fail-safe.

        Part VII: when the Brain opens a campaign, snapshot the market state that
        justified it so a future reasoning pass can retrieve it as an analogue
        once the outcome is known. Observational — runs in every origination mode
        (memory never drives execution).
        """
        try:
            decision = getattr(output, "decision", None)
            campaign = getattr(output, "campaign", None)
            if decision is None or campaign is None:
                return
            if getattr(decision, "decision_type", None) != DecisionType.OPEN_CAMPAIGN:
                return
            direction = str(getattr(output, "direction", "") or "").upper()
            symbol = str(getattr(campaign, "symbol", "") or "")
            if direction not in ("LONG", "SHORT") or not symbol:
                return
            from cognition.memory import fingerprint_from_market_state  # local, fail-safe
            fp = fingerprint_from_market_state(market_state, now=now)
            spec = campaign.to_dict() if hasattr(campaign, "to_dict") else {}
            # Part VIII — record which evidence sources leaned the campaign's way,
            # so the influence ledger can credit/debit them once the outcome lands.
            try:
                want = 1.0 if direction == "LONG" else -1.0
                sources = sorted({
                    e.source_module for e in market_state.fresh_evidence(now)
                    if e.source_module and (e.polarity * want) > 0.05
                })
                if sources:
                    spec = dict(spec)
                    spec["supporting_sources"] = sources
            except Exception:  # noqa: BLE001
                pass
            self._memory.record_open(
                symbol=symbol, direction=direction, fingerprint=fp,
                campaign_id=str(getattr(campaign, "campaign_id", "") or ""), spec=spec,
            )
            self._memory_opens += 1
        except Exception as exc:  # noqa: BLE001 — memory must never break the loop
            logger.debug("[cognition-loop] record-open-memory fault: %s", exc)

    def _maybe_originate(self, output: Any, *, now: Optional[float] = None) -> None:
        """Originate an entry from the Brain's CampaignSpecification. Fail-safe.

        Constitution Part VI Art 2: the Brain ORIGINATES campaigns; execution
        realises them. This fires only on a fresh ``OPEN_CAMPAIGN`` decision that
        carries a directional campaign the book does not already hold. In
        ``shadow`` mode it records the intended order and submits nothing; in
        ``live`` mode it hands a translated :class:`OriginationIntent` to the
        wired sink (and degrades to shadow-record when no sink is wired).
        """
        try:
            decision = getattr(output, "decision", None)
            campaign = getattr(output, "campaign", None)
            if decision is None or campaign is None:
                return
            if getattr(decision, "decision_type", None) != DecisionType.OPEN_CAMPAIGN:
                return
            direction = str(getattr(output, "direction", "") or "").upper()
            symbol = str(getattr(campaign, "symbol", "") or "")
            if direction not in ("LONG", "SHORT") or not symbol:
                return
            if (symbol, direction) in self._open_keys:
                return
            balance = 0.0
            if self._balance_provider is not None:
                try:
                    balance = float(self._balance_provider(symbol) or 0.0)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[cognition-loop] balance provider fault: %s", exc)
                    balance = 0.0
            intent = _translate(
                campaign,
                balance=balance,
                risk_fraction=self._risk_fraction,
                max_exposure=self._max_exposure,
            )
            if intent is None:
                return
            # Suppress repeats within a cycle regardless of submission outcome.
            self._open_keys.add((symbol, direction))
            if self.origination_mode == "live" and self._origination_sink is not None:
                self._origination_sink(intent)
                self._orig_submitted += 1
                logger.info(
                    "[cognition-loop] originated LIVE %s %s (stake=%s)",
                    symbol, direction, intent.stake_usd,
                )
            else:
                self._orig_intended += 1
                logger.info(
                    "[cognition-loop] originated SHADOW %s %s (stake=%s)",
                    symbol, direction, intent.stake_usd,
                )
        except Exception as exc:  # noqa: BLE001 — origination must never break the loop
            logger.debug("[cognition-loop] originate fault: %s", exc)

    def _manage_open_positions(self, *, now: Optional[float] = None) -> int:
        """Produce a Brain management decision for each open position. Fail-safe."""
        if self._position_source is None or not hasattr(self._brain, "manage"):
            return 0
        try:
            positions = list(self._position_source() or [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition-loop] position source fault: %s", exc)
            return 0
        managed = 0
        for pos in positions[: self.max_symbols_per_cycle]:
            try:
                symbol = getattr(pos, "symbol", "") or ""
                ms = self._consolidator.build(symbol, now=now)
                self._brain.manage(pos, ms, now=now)
                managed += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("[cognition-loop] manage fault: %s", exc)
                continue
        self._managed += managed
        return managed

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 — the loop must never die
                logger.debug("[cognition-loop] cycle fault: %s", exc)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=self._name)
        self._thread.start()
        logger.info(
            "[cognition-loop] started (interval=%.0fs, shadow=%s)",
            self.interval_seconds, self.shadow_mode,
        )

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "shadow_mode": self.shadow_mode,
            "interval_seconds": self.interval_seconds,
            "cycles": self._cycles,
            "decisions": self._decisions,
            "managed": self._managed,
            "origination_mode": self.origination_mode,
            "origination_sink_wired": self._origination_sink is not None,
            "orig_intended": self._orig_intended,
            "orig_submitted": self._orig_submitted,
            "memory_enabled": self._memory is not None,
            "memory_opens": self._memory_opens,
            "operations_enabled": (self._operations_author is not None
                                   and getattr(self._operations_author, "enabled", False)),
            "ops_submitted": self._ops_submitted,
        }


__all__ = ["EvidenceConsolidator", "BrainActionBridge", "CognitionLoop", "SymbolsProvider"]

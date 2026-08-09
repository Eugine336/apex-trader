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
import time as _time
from typing import Any, Callable, Optional

from cognition.campaign_translator import translate as _translate
from cognition.contracts import DecisionType, Evidence, EvidenceDomain, MarketState
from cognition.management_translator import translate_management as _translate_management
from cognition.management_translator import ManagementAction as _ManagementAction
from cognition.evidence_adapters import (
    evidence_from_analogues,
    evidence_from_developing_bias,
    evidence_from_execution_cost,
    evidence_from_portfolio,
    evidence_from_reasoning,
    evidence_from_thesis_status,
    evidence_from_votes,
)

logger = logging.getLogger("apex.cognition.loop")

SymbolsProvider = Callable[[], "list[str]"]

# Part XXI Art 6 — the Brain requests a capability, not a vendor. The cognition
# read is strategic market reasoning; the orchestrator maps it to providers.
_REASONING_CAPABILITY = "strategic_reasoning"


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


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
        developing_source: Optional[Callable[[str], Any]] = None,
        per_module: bool = True,
        memory: Optional[Any] = None,
        max_analogues: int = 5,
        influence: Optional[Any] = None,
        influence_enabled: bool = False,
        reasoning: Optional[Any] = None,
        knowledge: Optional[Any] = None,
        execution_cost_source: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._ctx = ctx
        self._vote_source = vote_source
        self._developing_source = developing_source
        self._per_module = bool(per_module)
        self._memory = memory
        self._max_analogues = max(1, int(max_analogues))
        self._influence = influence
        self._influence_enabled = bool(influence_enabled)
        self._reasoning = reasoning
        self._knowledge = knowledge
        # Violation V9 — a ``symbol -> cost`` callable estimating the per-symbol
        # round-trip execution cost (spread + slippage) in R-multiples. Surfaced
        # as EXECUTION_QUALITY Evidence so the Brain's EV is net of execution
        # cost (Part XXIV/XXV). May return a float (total cost in R) or a dict
        # with estimated_spread_r / estimated_slippage_r / estimated_total_cost_r.
        self._execution_cost_source = execution_cost_source
        self._portfolio_source: Optional[Callable[[], Any]] = None
        self._price_source: Optional[Callable[[str], Any]] = None
        # Part XXI Art 9/10/11 — records each council consultation + grades each
        # advisor. Observability/learning only; never authority. Fail-safe.
        self._consult_ledger: Optional[Any] = None

    def set_vote_source(self, vote_source: Optional[Callable[[str], Any]]) -> None:
        """Wire (or clear) the live WorldModel vote-panel source.

        The bootstrap supplies this once the WorldModel store exists so the
        consolidator can turn each symbol's live per-module vote panel into
        domain-classified Evidence. Fail-safe callable — never invoked eagerly.
        """
        self._vote_source = vote_source

    def set_developing_source(self, developing_source: Optional[Callable[[str], Any]]) -> None:
        """Wire (or clear) the DEVELOPING (forming-bar) bias source.

        Returns the developing WorldModel's bias dict for a symbol; the
        consolidator surfaces it as one short-lived multi-timeframe Evidence so
        the Brain also sees the fresher between-close read. Fail-safe callable.
        """
        self._developing_source = developing_source

    def set_portfolio_source(self, portfolio_source: Optional[Callable[[], Any]]) -> None:
        """Wire (or clear) the live portfolio-assessment source (Part XVIII Art 11).

        A zero-arg callable returning the campaign registry's
        ``portfolio_assessment()`` dict. The consolidator turns the correlated
        clusters / concentration relevant to each symbol into portfolio-context
        Evidence so the Brain reasons every symbol against the whole book.
        Fail-safe callable — never invoked eagerly.
        """
        self._portfolio_source = portfolio_source

    def set_price_source(self, price_source: Optional[Callable[[str], Any]]) -> None:
        """Wire (or clear) the live price-snapshot source (Part XIX Art 2).

        A ``symbol -> list[Evidence]`` callable that reconstructs the actual
        market picture — recent multi-timeframe OHLC, live price/spread and the
        open position — so the Brain observes the chart like a trader, not only
        pre-digested module verdicts. Fail-safe callable; never invoked eagerly.
        """
        self._price_source = price_source

    def set_consultation_ledger(self, ledger: Optional[Any]) -> None:
        """Wire (or clear) the Advisory-Council consultation ledger (Part XXI).

        The bootstrap supplies this so every reasoning-orchestrator consultation
        is recorded and each advisor is graded (records + scorecards, Art 9/10/
        11). Observability/learning only — it never influences selection or the
        Brain's decision. Fail-safe; never invoked eagerly.
        """
        self._consult_ledger = ledger

    def set_execution_cost_source(self, execution_cost_source: Optional[Callable[[str], Any]]) -> None:
        """Wire (or clear) the per-symbol execution-cost source (Violation V9).

        A ``symbol -> cost`` callable returning the estimated round-trip cost in
        R-multiples (a float total, or a dict with estimated_spread_r /
        estimated_slippage_r / estimated_total_cost_r). The consolidator surfaces
        it as EXECUTION_QUALITY Evidence so the Brain's EV is net of execution
        cost (Part XXIV/XXV). Fail-safe callable; never invoked eagerly.
        """
        self._execution_cost_source = execution_cost_source

    def _consult(self, symbol: str, payload: dict, now: Optional[float],
                 max_engines: Optional[int]) -> Any:
        """Consult the reasoning subsystem with capability + adaptive depth.

        Prefers the Reasoning Orchestrator's rich signature (capability +
        ``max_engines``); falls back to the basic ``consult(symbol, payload,
        now=)`` for any duck-typed reasoner that doesn't accept them. Fail-safe
        at the call site (the caller wraps this in try/except)."""
        reasoning = self._reasoning
        try:
            return reasoning.consult(
                symbol, payload, now=now,
                capability=_REASONING_CAPABILITY, max_engines=max_engines,
            )
        except TypeError:
            return reasoning.consult(symbol, payload, now=now)

    def build(
        self,
        symbol: str,
        *,
        injected: Optional[list] = None,
        now: Optional[float] = None,
    ) -> MarketState:
        ms = MarketState(symbol=str(symbol or ""))
        # Article XXXIV — silent failure is unacceptable. An infrastructure fault
        # in a source degrades the evidence picture; that is a WARNING, not a
        # debug detail, and it is surfaced back into the MarketState as a
        # zero-confidence / max-uncertainty integrity Evidence so the Brain can
        # tell "no opportunity exists" apart from "cannot determine whether one
        # exists". ``failed_sources`` counts the guarded sources that faulted;
        # ``attempted_sources`` counts those that were wired and tried.
        failed_sources: list[str] = []
        attempted_sources = 0

        def _integrity(observation: str) -> None:
            ms.add(Evidence(
                source_module="consolidator.integrity",
                domain=EvidenceDomain.OTHER, symbol=ms.symbol,
                observation=observation, confidence=0.0, uncertainty=1.0,
            ))

        try:
            if injected:
                for e in injected:
                    ms.add(e)
            # Part XIX Art 2 — observe reality first: reconstruct the live chart
            # (multi-timeframe OHLC + price/spread + open position) so the Brain
            # sees the actual market, not only derived module verdicts. Added
            # first so the price picture leads the evidence the reasoner reads.
            if self._price_source is not None:
                attempted_sources += 1
                try:
                    for e in self._price_source(ms.symbol) or []:
                        ms.add(e)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[consolidator] price source FAILED (%s): %s", symbol, exc)
                    failed_sources.append("price")
                    _integrity("PRICE DATA UNAVAILABLE — cannot determine market state")
            ctx = self._ctx
            engine = getattr(ctx, "thesis_engine", None) if ctx is not None else None
            if engine is not None:
                status = engine.get_status() or {}
                for e in evidence_from_thesis_status(status, ms.symbol):
                    # When per-module is off, keep only the aggregate read.
                    if self._per_module or e.source_module == "brain.thesis_engine":
                        ms.add(e)
            if self._vote_source is not None:
                attempted_sources += 1
                try:
                    for e in evidence_from_votes(ms.symbol, self._vote_source(ms.symbol)):
                        ms.add(e)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[consolidator] vote source FAILED (%s): %s", symbol, exc)
                    failed_sources.append("votes")
                    _integrity("MODULE OBSERVATIONS UNAVAILABLE")
            if self._developing_source is not None:
                try:
                    for e in evidence_from_developing_bias(
                        ms.symbol, self._developing_source(ms.symbol),
                    ):
                        ms.add(e)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[consolidator] developing source fault (%s): %s", symbol, exc)
            # Violation V9 — surface the per-symbol estimated round-trip execution
            # cost (spread + slippage, in R) as EXECUTION_QUALITY Evidence so the
            # Brain's EV is net of execution cost (Part XXIV/XXV). A theoretical
            # thesis that cannot clear its cost is then declined at the EV gate.
            if self._execution_cost_source is not None:
                try:
                    for e in evidence_from_execution_cost(
                        ms.symbol, self._execution_cost_source(ms.symbol),
                    ):
                        ms.add(e)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[consolidator] execution-cost source fault (%s): %s", symbol, exc)
            # Part XVIII Art 11 — portfolio intelligence: surface how this
            # symbol's exposure stacks against the live book (correlated
            # clusters, concentration) as risk-context Evidence (never a vote).
            if self._portfolio_source is not None:
                attempted_sources += 1
                try:
                    for e in evidence_from_portfolio(ms.symbol, self._portfolio_source()):
                        ms.add(e)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[consolidator] portfolio source FAILED (%s): %s", symbol, exc)
                    failed_sources.append("portfolio")
                    _integrity("PORTFOLIO DATA UNAVAILABLE — exposure unknown")
            # advisory Evidence (never a vote). The Brain synthesises them.
            if self._reasoning is not None:
                attempted_sources += 1
                try:
                    if getattr(self._reasoning, "available", False):
                        cons = ms.consolidation(now)
                        payload = {
                            "consolidation": cons,
                            "evidence": [e.to_dict() for e in ms.fresh_evidence(now)[:64]],
                        }
                        # Part XXIV panel mode — every available advisor advises
                        # in parallel (depth 0 = uncapped). Otherwise Part XXI
                        # Art 8 adaptive depth scales advisors to the difficulty
                        # of the read: a clear picture needs one advisor, an
                        # uncertain/conflicted one several, a critical one all.
                        need = max(_f(cons.get("aggregate_uncertainty", 1.0)),
                                   _f(cons.get("conflict_ratio", 0.0)))
                        if getattr(self._reasoning, "panel", False):
                            depth: Optional[int] = 0      # full panel — all advisors
                        elif need < 0.34:
                            depth = 1
                        elif need < 0.67:
                            depth = 2
                        else:
                            depth = 0                      # critical ⇒ consult all
                        consult = self._consult(ms.symbol, payload, now, depth)
                        for e in evidence_from_reasoning(ms.symbol, consult):
                            ms.add(e)
                        if self._consult_ledger is not None and consult is not None:
                            self._consult_ledger.record(
                                consult, uncertainty=need,
                                capability=_REASONING_CAPABILITY, now=now,
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[consolidator] reasoning consult FAILED (%s): %s", symbol, exc)
                    failed_sources.append("reasoning")
            # Part IX v3.0 — the Operational Intelligence Layer: external market
            # context, institutional research and AI advisors reached through
            # Composio become advisory Evidence (never a vote). Read-only, gated,
            # throttled and self-measuring; a fault degrades to no evidence.
            if self._knowledge is not None:
                attempted_sources += 1
                try:
                    for e in self._knowledge.evidence_for(ms.symbol, now=now):
                        ms.add(e)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[consolidator] knowledge source FAILED (%s): %s", symbol, exc)
                    failed_sources.append("knowledge")
            # Part VII — consult institutional memory: surface similar past
            # campaigns and their outcomes as a historical-analogue Evidence.
            if self._memory is not None:
                attempted_sources += 1
                try:
                    analogues = self._memory.find_analogues(ms, limit=self._max_analogues)
                    for e in evidence_from_analogues(ms.symbol, analogues):
                        ms.add(e)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[consolidator] memory recall FAILED (%s): %s", symbol, exc)
                    failed_sources.append("memory")
                    _integrity("INSTITUTIONAL MEMORY UNAVAILABLE")
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
            # Article XXXIV — when more than two sources failed, the whole picture
            # is compromised: surface a meta-integrity Evidence so the Brain knows
            # it is reasoning on an incomplete picture, not a quiet market.
            if len(failed_sources) > 2:
                logger.warning(
                    "[consolidator] EVIDENCE INTEGRITY DEGRADED (%s): %d of %d sources failed: %s",
                    symbol, len(failed_sources), attempted_sources, ", ".join(failed_sources),
                )
                _integrity(
                    f"EVIDENCE INTEGRITY DEGRADED: {len(failed_sources)} of "
                    f"{attempted_sources} sources failed — reasoning on incomplete picture"
                )
        except Exception as exc:  # noqa: BLE001 — consolidation must never break
            logger.warning("[consolidator] build(%s) FAILED with a fault: %s", symbol, exc)
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
        management_mode: str = "shadow",
        event_driven: bool = False,
        event_min_interval_seconds: float = 8.0,
        event_confidence_delta: float = 0.15,
        clock: Optional[Callable[[], float]] = None,
        campaign_registry: Optional[Any] = None,
        reallocation_enabled: bool = False,
        reallocation_max_per_component: int = 2,
        reallocation_concentration_limit: float = 0.6,
        reallocation_trim_fraction: float = 0.5,
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
        self._is_market_open: Optional[Callable[[str], bool]] = None
        self._name = str(name or "cognition-loop")
        # Management realisation (Part VI): the mode mirrors origination —
        # ``off`` never manages, ``shadow`` records the intended action, ``live``
        # hands it to the wired sink for execution. Default shadow (safe).
        m2 = str(management_mode or "shadow").strip().lower()
        self.management_mode = m2 if m2 in ("off", "shadow", "live") else "shadow"
        self._management_sink: Optional[Callable[[Any, Any], None]] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Event-driven "breathing" (Part XII): wake the Brain the instant the
        # market meaningfully shifts, instead of only on the fixed interval. The
        # periodic cycle remains a backstop. Off by default (pure interval).
        self.event_driven = bool(event_driven)
        self.event_min_interval_seconds = max(0.0, float(event_min_interval_seconds))
        self.event_confidence_delta = max(0.0, float(event_confidence_delta))
        self._clock = clock or _time.monotonic
        self._wake = threading.Event()
        self._pending: set = set()
        self._pending_lock = threading.Lock()
        self._event_last_magnitude: dict = {}
        self._event_last_reason_at: dict = {}
        self._cycles = 0
        self._decisions = 0
        # Round-robin cursor so the periodic cycle rotates through the FULL
        # symbol universe across successive cycles instead of only ever reasoning
        # the first max_symbols_per_cycle symbols (which starves the rest).
        self._cycle_offset = 0
        self._managed = 0
        self._orig_intended = 0
        self._orig_submitted = 0
        self._memory_opens = 0
        self._ops_submitted = 0
        self._event_reasons = 0
        self._event_throttled = 0
        self._manage_intended = 0
        self._manage_submitted = 0
        # Part XVIII Art 13 — management follows evidence, not clocks. A per-
        # symbol floor (reusing event_min_interval_seconds) prevents a fast feed
        # from re-managing the same campaign on every micro-event, while the
        # periodic cycle still acts as a backstop.
        self._last_manage_at: dict = {}
        # Part XVIII Art 1/8/12 — the Brain owns the campaign lifecycle. The loop
        # drives this registry: births/refreshes campaigns as it originates and
        # manages, feeds portfolio context back as Evidence (Art 11), and re-arms
        # reasoning the instant a campaign closes so the Brain immediately asks
        # "does the opportunity still exist?" (Art 8 scalp re-entry).
        self._campaigns = campaign_registry
        self._known_open: set = set()
        # Part XVIII Art 11 — portfolio capital reallocation. When the live book
        # is over-concentrated in one correlated leg, trim the weakest campaign
        # in that cluster (ranked by the Brain's own health) to free capital for
        # higher-EV opportunities. Off unless explicitly enabled; throttled so a
        # trim is not re-issued every cycle.
        self.reallocation_enabled = bool(reallocation_enabled)
        self._realloc_max_per_component = max(1, int(reallocation_max_per_component))
        self._realloc_concentration_limit = float(reallocation_concentration_limit)
        self._realloc_trim_fraction = min(0.95, max(0.05, float(reallocation_trim_fraction)))
        self._last_realloc_at: dict = {}
        self._reallocations = 0
        if self._campaigns is not None:
            try:
                self._consolidator.set_portfolio_source(
                    self._campaigns.portfolio_assessment
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[cognition-loop] portfolio source wiring fault: %s", exc)
        self._open_keys: set = set()

    def set_origination_sink(self, sink: Optional[Callable[[Any], None]]) -> None:
        """Wire the live order-submission sink (set by the system that owns the
        executor). When unset, ``live`` origination degrades to shadow-record."""
        self._origination_sink = sink

    def set_management_sink(self, sink: Optional[Callable[[Any, Any], None]]) -> None:
        """Wire the live management-execution sink.

        Called with ``(ManagementAction, PositionView)``; the owner resolves the
        broker position and submits the close/partial/modify/reverse/scale-in.
        When unset, ``live`` management degrades to shadow-record. Fail-safe."""
        self._management_sink = sink

    def set_vote_source(self, vote_source: Optional[Callable[[str], Any]]) -> None:
        """Wire the live WorldModel vote-panel source onto the consolidator.

        Delegates to :meth:`EvidenceConsolidator.set_vote_source`. The owning
        system calls this once its WorldModel store exists, so the Brain reasons
        over the live per-module market read instead of an empty MarketState."""
        try:
            self._consolidator.set_vote_source(vote_source)
        except Exception as exc:  # noqa: BLE001 — wiring must never break startup
            logger.debug("[cognition-loop] set_vote_source ignored a fault: %s", exc)

    def set_developing_source(self, developing_source: Optional[Callable[[str], Any]]) -> None:
        """Wire the DEVELOPING (forming-bar) bias source onto the consolidator.

        Delegates to :meth:`EvidenceConsolidator.set_developing_source` so the
        Brain also sees the fresher between-close read alongside the confirmed
        vote panel. Fail-safe — a wiring fault must never break startup."""
        try:
            self._consolidator.set_developing_source(developing_source)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition-loop] set_developing_source ignored a fault: %s", exc)

    def set_portfolio_source(self, portfolio_source: Optional[Callable[[], Any]]) -> None:
        """Wire the portfolio-assessment source onto the consolidator (Art 11).

        Normally self-wired from the injected campaign registry; exposed so the
        owner can override. Fail-safe — a wiring fault never breaks startup."""
        try:
            self._consolidator.set_portfolio_source(portfolio_source)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition-loop] set_portfolio_source ignored a fault: %s", exc)

    def set_price_source(self, price_source: Optional[Callable[[str], Any]]) -> None:
        """Wire the live price-snapshot source onto the consolidator (Part XIX Art 2).

        Lets the Brain observe the actual multi-timeframe chart + live price +
        open position. Fail-safe — a wiring fault never breaks startup."""
        try:
            self._consolidator.set_price_source(price_source)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition-loop] set_price_source ignored a fault: %s", exc)

    def set_consultation_ledger(self, ledger: Optional[Any]) -> None:
        """Wire the Advisory-Council consultation ledger onto the consolidator.

        Delegates to :meth:`EvidenceConsolidator.set_consultation_ledger` so
        every reasoning-orchestrator consultation is recorded and each advisor
        graded (Part XXI Art 9/10/11). Fail-safe — never breaks startup."""
        try:
            self._consolidator.set_consultation_ledger(ledger)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition-loop] set_consultation_ledger ignored a fault: %s", exc)

    def set_market_open_source(self, is_market_open: Optional[Callable[[str], bool]]) -> None:
        """Wire the broker-truth market-open check the EntryOrchestrator already
        uses, so the reasoning council is skipped for a symbol whose market is
        closed instead of only being consulted after a full 14-provider fan-out.

        Weekend/holiday closures otherwise burn the full advisory-council token
        budget (and every provider's rate limit) on instruments that could
        never be traded anyway, since the market-open gate previously only
        applied at order-placement time. Fail-safe — a missing or faulting
        source means every symbol is reasoned over, matching prior behavior."""
        self._is_market_open = is_market_open

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
        for symbol in self._select_cycle_batch(symbols):
            made += self._reason_over_symbol(symbol, now=now)
        self._cycles += 1
        self._decisions += made
        self._manage_open_positions(now=now)
        self._maybe_author_operations(now=now)
        return made

    def _select_cycle_batch(self, symbols: list) -> list:
        """Round-robin the periodic cycle across the FULL symbol universe.

        The periodic backstop reasons at most ``max_symbols_per_cycle`` symbols
        per cycle to bound LLM cost — but it must not always pick the SAME first
        N, which starves every symbol past N (they would then only ever be
        reasoned by an event-driven wake, and no wake fires on a weekend/quiet
        feed). This advances a cursor each cycle so successive cycles cover the
        whole list (wrapping around), giving every symbol periodic coverage.
        Open-position management is separate (``_manage_open_positions``) and
        always covers all live positions regardless of this batch.
        """
        n = len(symbols)
        cap = self.max_symbols_per_cycle
        if n == 0 or cap <= 0:
            return []
        if cap >= n:
            self._cycle_offset = 0
            return list(symbols)
        off = self._cycle_offset % n
        batch = symbols[off:off + cap]
        if len(batch) < cap:  # wrap around the end of the list
            batch = batch + symbols[: cap - len(batch)]
        self._cycle_offset = (off + cap) % n
        return batch

    def _reason_over_symbol(self, symbol: str, *, now: Optional[float] = None) -> int:
        """Consolidate + reason over one symbol. Returns 1 on a decision, else 0.

        The shared per-symbol body used by both the periodic cycle and the
        event-driven path. Fail-safe: one symbol's fault never propagates.
        """
        is_open_check = self._is_market_open
        if is_open_check is not None:
            try:
                if not is_open_check(symbol):
                    logger.debug(
                        "[cognition-loop] %s market closed — skipping reasoning "
                        "(origination, not management)", symbol,
                    )
                    return 0
            except Exception as exc:  # noqa: BLE001
                # Fail-open: a faulting check must never block reasoning.
                logger.debug("[cognition-loop] market-open check faulted for %s: %s", symbol, exc)
        try:
            ms = self._consolidator.build(symbol, now=now)
            output = self._brain.reason(ms, now=now)
            # Visibility (Part XII): surface the one reasoner's decision at INFO
            # so the Brain's live reasoning is observable in the logs — not just
            # when it originates a trade.
            try:
                _dec = output.decision
                _dtype = getattr(getattr(_dec, "decision_type", None), "value", None) \
                    or str(getattr(_dec, "decision_type", "?"))
                logger.info(
                    "[cognition] %s -> %s (dir=%s conf=%.2f) reasoner=%s",
                    symbol, _dtype, getattr(output, "direction", "?"),
                    float(getattr(_dec, "confidence", 0.0) or 0.0),
                    "live" if getattr(self._brain, "available", False) else "unavailable",
                )
            except Exception:  # noqa: BLE001 — logging must never break the cycle
                pass
            if self._action_bridge is not None:
                self._action_bridge.on_decision(output)
            # Article XXI — degraded cognition must be recognised operationally:
            # when the Brain attenuated its confidence because most of the
            # council was absent, surface it at WARNING (not just debug) so a
            # partial-panel decision is visible in operational logs.
            try:
                _qa = getattr(output.decision, "questions_answered", {}) or {}
                _deg = str(_qa.get("cognitive_degradation", "") or "")
                if _deg.startswith("ACTIVE"):
                    logger.warning("[cognition] %s DEGRADED COGNITION — %s", symbol, _deg)
            except Exception as exc:  # noqa: BLE001 — logging must never break the cycle
                logger.debug("[cognition-loop] degradation-log fault (%s): %s", symbol, exc)
            if self._memory is not None:
                self._record_open_memory(output, ms, now=now)
            if self.origination_mode != "off":
                self._maybe_originate(output, now=now)
            # Part XVIII Art 13 — management is continuous and event-driven, not
            # clock-gated: the instant a symbol's evidence is re-reasoned (on a
            # periodic pass OR an event wake), immediately manage that symbol's
            # open campaign too. The periodic _manage_open_positions remains a
            # backstop for positions whose symbol wasn't in this pass's slice.
            if self.management_mode != "off":
                self._manage_symbol(symbol, now=now)
            return 1
        except Exception as exc:  # noqa: BLE001 — one symbol must not stop the loop
            logger.warning("[cognition-loop] reason(%s) FAILED: %s", symbol, exc)
            return 0

    def reason_symbol_now(self, symbol: str, *, now: Optional[float] = None) -> int:
        """Reason over a single symbol immediately (event-driven wake).

        Refreshes the open-book view first so origination suppression is correct,
        then reasons over the one symbol. Runs on the loop thread (never the
        caller's), keeping any LLM latency off the market-data path. Fail-safe.
        """
        try:
            self._open_keys = self._current_open_keys()
        except Exception:  # noqa: BLE001
            pass
        made = self._reason_over_symbol(symbol, now=now)
        self._decisions += made
        self._event_reasons += made
        return made

    def maybe_reason_on_change(
        self, symbol: str, change_magnitude: Any = None, *, now: Optional[float] = None,
    ) -> bool:
        """Queue an immediate Brain reason when a symbol's market read changes
        materially — NON-DIRECTIONALLY.

        The wake is driven by a raw *change magnitude* (e.g. a forming-bar
        activity/conviction level), never by a precomputed LONG/SHORT direction:
        cognition must not be gated on a directional pre-read (Constitution
        §XXIV event-driven integrity / §VII Q25 — opportunity discovery is not
        gated by a precomputed direction). Triggers on the first observation, on
        a magnitude move ≥ ``event_confidence_delta`` versus the last seen value,
        or on a bare nudge (``change_magnitude is None``), subject to a per-symbol
        floor (``event_min_interval_seconds``). The reasoning runs on the loop
        thread (this only enqueues + wakes it). No-op unless ``event_driven``.
        Fail-safe. Returns True when a reason was queued.
        """
        if not self.event_driven:
            return False
        try:
            sym = str(symbol or "")
            if not sym:
                return False
            try:
                m = None if change_magnitude is None else float(change_magnitude)
            except (TypeError, ValueError):
                m = None
            prev = self._event_last_magnitude.get(sym)
            self._event_last_magnitude[sym] = m
            if m is None or prev is None:
                triggered = True
            else:
                triggered = abs(m - prev) >= self.event_confidence_delta
            if not triggered:
                return False
            t = self._clock() if now is None else float(now)
            last = self._event_last_reason_at.get(sym, 0.0)
            if self.event_min_interval_seconds > 0.0 and (t - last) < self.event_min_interval_seconds:
                self._event_throttled += 1
                return False
            self._event_last_reason_at[sym] = t
            with self._pending_lock:
                self._pending.add(sym)
            self._wake.set()
            return True
        except Exception as exc:  # noqa: BLE001 — a trigger must never break the caller
            logger.debug("[cognition-loop] maybe_reason_on_change(%s) fault: %s", symbol, exc)
            return False

    def _drain_pending(self) -> list:
        with self._pending_lock:
            pend = list(self._pending)[: self.max_symbols_per_cycle]
            self._pending.clear()
        return pend

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
            # Part VIII/XXV — record which evidence sources informed the campaign
            # (provenance) so the influence ledger can credit/debit them once the
            # outcome lands. Evidence no longer carries a directional lean, so we
            # record every fresh contributing source, not "those that leaned our
            # way" (which would require a pre-cognition direction).
            try:
                sources = sorted({
                    e.source_module for e in market_state.fresh_evidence(now)
                    if e.source_module
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
                # Part XVIII Art 1 — a trade begins a campaign. Birth (or revive)
                # the campaign for this symbol/direction and record the opening
                # leg so the Brain manages it as one campaign spanning positions.
                self._campaign_open(symbol, direction, campaign, decision)
            else:
                self._orig_intended += 1
                logger.info(
                    "[cognition-loop] originated SHADOW %s %s (stake=%s)",
                    symbol, direction, intent.stake_usd,
                )
        except Exception as exc:  # noqa: BLE001 — origination must never break the loop
            logger.debug("[cognition-loop] originate fault: %s", exc)

    def _manage_open_positions(self, *, now: Optional[float] = None) -> int:
        """Produce a Brain management decision for each open position. Fail-safe.

        The periodic backstop (Part XVIII Art 13): guarantees every open campaign
        is re-reasoned each cycle even when its symbol was not in this cycle's
        reasoning slice. Event-driven per-symbol management (``_manage_symbol``,
        called from ``_reason_over_symbol``) does the fast, evidence-driven work;
        the shared throttle keeps the two from double-managing the same campaign.
        """
        if self._position_source is None or not hasattr(self._brain, "manage"):
            return 0
        try:
            positions = list(self._position_source() or [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition-loop] position source fault: %s", exc)
            return 0
        managed = 0
        for pos in positions:
            managed += self._manage_one(pos, now=now)
        # Part XVIII Art 8 — the campaign is the unit: the instant a campaign's
        # position leaves the live book (closed), immediately re-arm reasoning so
        # the Brain asks "does the opportunity still exist? trade again?" rather
        # than waiting for the next interval. Uses the full-book snapshot this
        # backstop already holds.
        try:
            current = {
                str(getattr(p, "symbol", "") or "")
                for p in positions if getattr(p, "symbol", "")
            }
            disappeared = self._known_open - current
            self._known_open = current
            if disappeared and self.event_driven:
                with self._pending_lock:
                    self._pending |= disappeared
                self._wake.set()
                for sym in disappeared:
                    logger.info(
                        "[cognition-loop] campaign closed %s — re-arming "
                        "reasoning (Art 8 opportunity re-check)", sym,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition-loop] campaign re-arm fault: %s", exc)
        # Part XVIII Art 11 — after managing each campaign, reason across the
        # whole book and redistribute capital away from over-concentrated
        # correlated risk. Runs once per backstop cycle on the same snapshot.
        self._maybe_reallocate(positions, now=now)
        return managed

    def _maybe_reallocate(self, positions: Any, *, now: Optional[float] = None) -> int:
        """Trim the weakest campaign(s) in an over-concentrated correlated
        cluster to free capital (Part XVIII Art 11). Gated + throttled.

        Uses the campaign registry's health-ranked reallocation targets and
        realises each trim through the SAME management sink the Brain uses (no
        parallel executor). Fully fail-safe. Returns the number of trims issued.
        """
        reg = self._campaigns
        if (not self.reallocation_enabled or reg is None
                or self.management_mode != "live"
                or self._management_sink is None):
            return 0
        try:
            targets = reg.reallocation_targets(
                max_per_component=self._realloc_max_per_component,
                concentration_limit=self._realloc_concentration_limit,
                trim_fraction=self._realloc_trim_fraction,
            )
            if not targets:
                return 0
            pos_by_key = {}
            for p in positions or []:
                pos_by_key[(str(getattr(p, "symbol", "") or ""),
                            str(getattr(p, "direction", "") or "").upper())] = p
            t = self._clock() if now is None else float(now)
            floor = max(self.interval_seconds, self.event_min_interval_seconds)
            issued = 0
            for tgt in targets:
                sym = str(getattr(tgt, "symbol", "") or "")
                direction = str(getattr(tgt, "direction", "") or "").upper()
                pos = pos_by_key.get((sym, direction))
                if pos is None:
                    continue
                last = self._last_realloc_at.get((sym, direction), 0.0)
                if (t - last) < floor:
                    continue
                action = _ManagementAction(
                    symbol=sym, kind=str(getattr(tgt, "kind", "partial_close")),
                    direction=direction,
                    fraction=float(getattr(tgt, "fraction", 0.5) or 0.5),
                    confidence=0.0,
                    reason=str(getattr(tgt, "reason", "") or "portfolio_reallocation"),
                )
                self._management_sink(action, pos)
                self._last_realloc_at[(sym, direction)] = t
                self._reallocations += 1
                issued += 1
                logger.info(
                    "[cognition-loop] REALLOCATE %s %s %s — %s",
                    sym, direction, action.kind, action.reason,
                )
                if reg is not None:
                    try:
                        reg.record_management(
                            sym, direction, "reallocate",
                            confidence=0.0, uncertainty=0.0, now=now,
                        )
                    except Exception:  # noqa: BLE001
                        pass
            return issued
        except Exception as exc:  # noqa: BLE001 — reallocation must never break the loop
            logger.debug("[cognition-loop] reallocation fault: %s", exc)
            return 0

    def _campaign_open(self, symbol: str, direction: str, campaign: Any,
                       decision: Any) -> None:
        """Birth/refresh the registry campaign for a Brain-originated entry.

        Part XVIII Art 1 — a trade begins a campaign. Best-effort; a registry
        fault never blocks origination."""
        reg = self._campaigns
        if reg is None:
            return
        try:
            conf = float(
                getattr(campaign, "confidence", 0.0)
                or getattr(decision, "confidence", 0.0) or 0.0
            )
            ev = float(getattr(campaign, "expected_value", 0.0) or 0.0)
            exposure = float(getattr(campaign, "desired_exposure", 0.0) or 0.0)
            reg.observe_thesis(symbol, direction, True, ev_over_flat=ev, confidence=conf)
            reg.record_leg(symbol, direction, "open", size=exposure)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition-loop] campaign open fault (%s): %s", symbol, exc)

    def _campaign_record(self, pos: Any, output: Any, *, now: Optional[float] = None) -> None:
        """Fold a Brain management verdict into live campaign health.

        Part XVIII Art 12 — continuous health. Also records a scale-in leg when
        the verdict adds exposure (Art 1). Best-effort; never breaks management."""
        reg = self._campaigns
        if reg is None:
            return
        try:
            symbol = str(getattr(pos, "symbol", "") or "")
            direction = str(getattr(pos, "direction", "") or "")
            if not symbol or not direction:
                return
            decision = getattr(output, "decision", None)
            dtype = getattr(getattr(decision, "decision_type", None), "value", None) \
                or str(getattr(decision, "decision_type", "") or "")
            objective = str(dtype or "hold").strip().lower()
            conf = float(getattr(decision, "confidence", 0.0) or 0.0)
            unc = float(getattr(decision, "uncertainty", 0.0) or 0.0)
            profit_r = getattr(pos, "profit_r", None)
            ev = float(profit_r) if profit_r is not None else 0.0
            reg.record_management(
                symbol, direction, objective,
                confidence=conf, ev_over_flat=ev, uncertainty=unc, now=now,
            )
            if objective == "scale_in":
                reg.record_leg(
                    symbol, direction, "scale_in",
                    size=float(getattr(pos, "size", 0.0) or 0.0), now=now,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition-loop] campaign record fault: %s", exc)

    def _manage_symbol(self, symbol: str, *, now: Optional[float] = None) -> int:
        """Immediately manage every open campaign on ``symbol`` (event-driven).

        Called the instant a symbol is re-reasoned so management follows evidence
        rather than a timer (Part XVIII Art 13). Fail-safe.
        """
        if self._position_source is None or not hasattr(self._brain, "manage"):
            return 0
        sym = str(symbol or "")
        if not sym:
            return 0
        try:
            positions = list(self._position_source() or [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition-loop] position source fault: %s", exc)
            return 0
        managed = 0
        for pos in positions:
            if str(getattr(pos, "symbol", "") or "") != sym:
                continue
            managed += self._manage_one(pos, now=now)
        return managed

    def _manage_one(self, pos: Any, *, now: Optional[float] = None) -> int:
        """Re-reason + realise management for ONE open campaign. Fail-safe.

        Applies a per-symbol floor (``event_min_interval_seconds``) so the same
        campaign is not re-managed on every micro-event or by both the event
        path and the periodic backstop within the same window. Returns 1 when a
        management decision was produced, else 0 (throttled / faulted)."""
        try:
            symbol = getattr(pos, "symbol", "") or ""
            t = self._clock() if now is None else float(now)
            if symbol and self.event_min_interval_seconds > 0.0:
                last = self._last_manage_at.get(symbol, 0.0)
                if (t - last) < self.event_min_interval_seconds:
                    return 0
            ms = self._consolidator.build(symbol, now=now)
            output = self._brain.manage(pos, ms, now=now)
            if symbol:
                self._last_manage_at[symbol] = t
            self._managed += 1
            # Part XVIII Art 12 — fold this verdict into live campaign health.
            self._campaign_record(pos, output, now=now)
            if self.management_mode != "off":
                self._realise_management(output, pos)
            return 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cognition-loop] manage FAILED: %s", exc)
            return 0

    def _realise_management(self, output: Any, position: Any) -> None:
        """Turn a Brain management verdict into a shadow record or a live action.

        ``shadow`` logs the intended action (observational, executes nothing);
        ``live`` hands it to the wired sink (degrading to shadow-record when no
        sink is set). Fail-safe — never breaks the management cycle."""
        try:
            action = _translate_management(output, position)
            if action is None:
                return  # HOLD / observe — nothing to do
            self._manage_intended += 1
            if self.management_mode == "live" and self._management_sink is not None:
                self._management_sink(action, position)
                self._manage_submitted += 1
                logger.info(
                    "[cognition-loop] MANAGE-LIVE %s %s (dir=%s conf=%.2f)",
                    action.symbol, action.kind, action.direction, action.confidence,
                )
            else:
                logger.info(
                    "[cognition-loop] MANAGE-SHADOW %s %s (dir=%s conf=%.2f) — %s",
                    action.symbol, action.kind, action.direction, action.confidence,
                    action.reason[:80],
                )
        except Exception as exc:  # noqa: BLE001 — management must never break the loop
            logger.debug("[cognition-loop] realise management fault: %s", exc)

    def _loop(self) -> None:
        # Pure-interval loop when event-driven is off — behaviourally unchanged.
        if not self.event_driven:
            while not self._stop.wait(self.interval_seconds):
                try:
                    self.run_once()
                except Exception as exc:  # noqa: BLE001 — the loop must never die
                    logger.debug("[cognition-loop] cycle fault: %s", exc)
            return
        # Event-driven loop: wake early on a nudge to reason the changed symbols,
        # and still run the full periodic cycle as a backstop every interval.
        poll = min(self.interval_seconds, 5.0)
        last_cycle = self._clock()
        while not self._stop.is_set():
            self._wake.wait(timeout=poll)
            if self._stop.is_set():
                break
            self._wake.clear()
            for sym in self._drain_pending():
                try:
                    self.reason_symbol_now(sym)
                except Exception as exc:  # noqa: BLE001 — the loop must never die
                    logger.debug("[cognition-loop] event reason(%s) fault: %s", sym, exc)
            nowm = self._clock()
            if (nowm - last_cycle) >= self.interval_seconds:
                try:
                    self.run_once()
                except Exception as exc:  # noqa: BLE001 — the loop must never die
                    logger.debug("[cognition-loop] cycle fault: %s", exc)
                last_cycle = nowm

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=self._name)
        self._thread.start()
        logger.info(
            "[cognition-loop] started (interval=%.0fs, shadow=%s, event_driven=%s)",
            self.interval_seconds, self.shadow_mode, self.event_driven,
        )

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        self._wake.set()
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
            "event_driven": self.event_driven,
            "event_reasons": self._event_reasons,
            "event_throttled": self._event_throttled,
            "management_mode": self.management_mode,
            "management_sink_wired": self._management_sink is not None,
            "manage_intended": self._manage_intended,
            "manage_submitted": self._manage_submitted,
            "campaign_registry_wired": self._campaigns is not None,
            "live_campaigns": (
                len(self._campaigns.live_campaigns())
                if self._campaigns is not None else 0
            ),
            "reallocation_enabled": self.reallocation_enabled,
            "reallocations": self._reallocations,
        }


__all__ = ["EvidenceConsolidator", "BrainActionBridge", "CognitionLoop", "SymbolsProvider"]

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

from cognition.brain import LONG, SHORT
from cognition.contracts import Evidence, EvidenceDomain, MarketState

logger = logging.getLogger("apex.cognition.loop")

SymbolsProvider = Callable[[], "list[str]"]


class EvidenceConsolidator:
    """Builds a consolidated :class:`MarketState` from live subsystem readings."""

    def __init__(self, ctx: Optional[Any] = None) -> None:
        self._ctx = ctx

    def build(
        self,
        symbol: str,
        *,
        injected: Optional["list[Evidence]"] = None,
        now: Optional[float] = None,
    ) -> MarketState:
        ms = MarketState(symbol=str(symbol or ""))
        try:
            if injected:
                for e in injected:
                    ms.add(e)
            self._add_thesis_evidence(ms)
        except Exception as exc:  # noqa: BLE001 — consolidation must never break
            logger.debug("[consolidator] build(%s) ignored a fault: %s", symbol, exc)
        return ms

    def _add_thesis_evidence(self, ms: MarketState) -> None:
        ctx = self._ctx
        engine = getattr(ctx, "thesis_engine", None) if ctx is not None else None
        if engine is None:
            return
        status = engine.get_status() or {}
        entry = (status.get("theses") or {}).get(ms.symbol)
        if not isinstance(entry, dict):
            return
        eff = entry.get("effective") or {}
        dominant = str(eff.get("dominant", "FLAT") or "FLAT").upper()
        long_ev = float(eff.get("long_ev", 0.0) or 0.0)
        short_ev = float(eff.get("short_ev", 0.0) or 0.0)
        flat_ev = float(eff.get("flat_ev", 0.0) or 0.0)
        if dominant == LONG:
            polarity = max(0.0, min(1.0, long_ev - flat_ev))
            conf = float((entry.get("long") or {}).get("confidence", 0.0) or 0.0)
        elif dominant == SHORT:
            polarity = -max(0.0, min(1.0, short_ev - flat_ev))
            conf = float((entry.get("short") or {}).get("confidence", 0.0) or 0.0)
        else:
            polarity = 0.0
            conf = float((entry.get("flat") or {}).get("confidence", 0.0) or 0.0)
        ms.add(Evidence(
            source_module="brain.thesis_engine",
            domain=EvidenceDomain.MULTI_TIMEFRAME,
            symbol=ms.symbol,
            observation=f"dominant competing thesis {dominant} (effective EV over flat)",
            confidence=conf,
            uncertainty=1.0 - conf,
            polarity=polarity,
            measurements={"long_ev": long_ev, "short_ev": short_ev, "flat_ev": flat_ev,
                          "dominant": dominant},
            relevance_horizon_seconds=900.0,
        ))


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
        name: str = "cognition-loop",
    ) -> None:
        self._brain = brain
        self._consolidator = consolidator
        self._symbols_provider = symbols_provider
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.max_symbols_per_cycle = max(1, int(max_symbols_per_cycle))
        self.shadow_mode = bool(shadow_mode)
        self._action_bridge = action_bridge
        self._name = str(name or "cognition-loop")
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cycles = 0
        self._decisions = 0

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
        made = 0
        for symbol in symbols[: self.max_symbols_per_cycle]:
            try:
                ms = self._consolidator.build(symbol, now=now)
                output = self._brain.reason(ms, now=now)
                made += 1
                if self._action_bridge is not None:
                    self._action_bridge.on_decision(output)
            except Exception as exc:  # noqa: BLE001 — one symbol must not stop the loop
                logger.debug("[cognition-loop] reason(%s) fault: %s", symbol, exc)
                continue
        self._cycles += 1
        self._decisions += made
        return made

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
        }


__all__ = ["EvidenceConsolidator", "BrainActionBridge", "CognitionLoop", "SymbolsProvider"]

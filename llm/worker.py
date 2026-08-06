"""APEX TRADER — LLM reasoning worker (off-hot-path invocation).

A blocking provider round-trip must NEVER run in the tick/analysis loop, so the
:class:`~llm.reasoner.LLMReasoner` is driven from this dedicated daemon thread.
On each cycle it pulls the current structured evidence for the tracked symbols
(from an injected ``evidence_source`` — typically the ThesisEngine status) and
asks the reasoner for an opinion. The reasoner records each opinion (surfaced in
Governance status) and, when ``drive_decisions`` is enabled, the consensus path
can read the latest opinion via :meth:`LLMReasoner.latest_opinion`.

Design principles:

* **Off the hot path.** Its own daemon thread; it never blocks trading. Uses a
  ``threading.Event`` for a promptly-interruptible sleep so shutdown is clean.
* **Inert when disabled.** :meth:`start` only spins a thread when the reasoner
  is actually ``available`` (enabled + a usable client). With no provider set it
  is a pure no-op — no thread, no calls.
* **Fail-safe + bounded.** Every cycle is wrapped; one symbol's fault never
  stops the loop, and ``max_symbols_per_cycle`` caps how many provider calls a
  single cycle can make.
* **Testable offline.** :meth:`run_once` is pure loop logic (no thread, no
  sleep, injectable clock) so the worker is fully verifiable without a network.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from loguru import logger

# A source of structured evidence: returns ``{symbol: evidence_dict}``.
EvidenceSource = Callable[[], "dict[str, dict]"]


class LLMReasoningWorker:
    """Background daemon that drives the LLM reasoner off the hot path."""

    def __init__(
        self,
        reasoner: Any,
        evidence_source: EvidenceSource,
        *,
        interval_seconds: float = 60.0,
        max_symbols_per_cycle: int = 8,
        name: str = "llm-reasoner",
    ) -> None:
        self._reasoner = reasoner
        self._evidence_source = evidence_source
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.max_symbols_per_cycle = max(1, int(max_symbols_per_cycle))
        self._name = str(name or "llm-reasoner")
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cycles = 0
        self._opinions = 0

    @property
    def available(self) -> bool:
        return bool(self._reasoner is not None
                    and getattr(self._reasoner, "available", False))

    @property
    def running(self) -> bool:
        return self._running

    def run_once(self, now: Optional[float] = None) -> int:
        """Run a single reasoning cycle. Returns the number of opinions formed.

        Pure loop logic (no thread, no sleep) so it is unit-testable offline.
        Fail-safe: any fault in the source or a single symbol is swallowed.
        """
        if not self.available:
            return 0
        try:
            sources = self._evidence_source() or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[llm-worker] evidence source fault: {}", exc)
            return 0
        formed = 0
        for symbol, evidence in list(sources.items())[: self.max_symbols_per_cycle]:
            try:
                opinion = self._reasoner.reason(symbol, evidence, now=now)
                if opinion is not None:
                    formed += 1
            except Exception as exc:  # noqa: BLE001 — one symbol must not stop the loop
                logger.debug("[llm-worker] reason({}) fault: {}", symbol, exc)
                continue
        self._cycles += 1
        self._opinions += formed
        return formed

    def _loop(self) -> None:
        # Interruptible sleep: wait() returns True the moment stop() is called.
        while not self._stop.wait(self.interval_seconds):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 — the loop must never die
                logger.debug("[llm-worker] cycle fault: {}", exc)

    def start(self) -> None:
        """Start the daemon — a no-op when the reasoner is unavailable."""
        if self._running:
            return
        if not self.available:
            logger.debug("[llm-worker] reasoner unavailable — worker not started")
            return
        self._running = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=self._name,
        )
        self._thread.start()
        logger.info(
            "[llm-worker] started (interval={:.0f}s, max_symbols/cycle={})",
            self.interval_seconds, self.max_symbols_per_cycle,
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
            "available": self.available,
            "interval_seconds": self.interval_seconds,
            "max_symbols_per_cycle": self.max_symbols_per_cycle,
            "cycles": self._cycles,
            "opinions_formed": self._opinions,
        }


__all__ = ["LLMReasoningWorker", "EvidenceSource"]

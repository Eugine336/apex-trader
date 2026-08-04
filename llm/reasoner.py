"""APEX TRADER — LLM reasoning subsystem.

The language model is **not** a chatbot bolted onto the side and it is **not**
an oracle that overrides deterministic safeguards. It is an additional reasoning
participant that receives the same *structured evidence* every other module sees
(the competing theses, the vote panel summary, regime, structure) and returns a
structured opinion: a direction, a calibrated confidence, a short rationale, the
competing hypotheses it considered, and the information it feels is missing.

That opinion is emitted as **evidence**, exactly like any analytical module — it
becomes a vote-like object whose influence is governed by the existing
performance-based authority layer (``ModuleGovernor`` / ``VoteCalibrator``) and
can never bypass the physics vetoes. Introduced observationally: default OFF,
and even when enabled it records its opinions for the dashboard/governance and
only feeds the panel once ``drive_decisions`` is set.

Design principles:

* **Fail-safe.** Every public method swallows faults and returns a safe default
  (``None`` / neutral) — a reasoning fault must never break the trading cycle.
* **Bounded / throttled.** LLM calls are expensive; a per-symbol minimum
  interval prevents hammering the provider. The network call itself lives behind
  :class:`llm.client.LLMClient` and is only reached when a real provider is
  configured — so this module is fully testable offline.
* **Secret-safe.** Status output exposes provider/model only, never the key.
* **Latency-aware.** :meth:`reason` performs a blocking provider call, so it must
  be driven from a background/periodic path — never from the hot tick loop.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"

_SYSTEM_PROMPT = (
    "You are the reasoning subsystem of an autonomous trading intelligence. "
    "You are given STRUCTURED EVIDENCE about one instrument: competing "
    "Long/Short/Flat theses with their expected value and confidence, a summary "
    "of module votes, market regime and structure. Reason over the evidence — "
    "do not invent data you were not given. Weigh the evidence, consider "
    "competing explanations, and state what would change your mind.\n\n"
    "Respond with STRICT JSON only, no prose, no markdown fences, exactly:\n"
    "{\"direction\": \"LONG|SHORT|FLAT\", \"confidence\": 0.0-1.0, "
    "\"rationale\": \"one or two sentences\", "
    "\"competing_hypotheses\": [\"...\"], \"missing_information\": [\"...\"]}\n"
    "FLAT means the evidence does not support acting. Confidence is your "
    "calibrated probability that the stated direction is correct."
)


def _clamp01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN
        return 0.0
    return min(1.0, max(0.0, f))


def _norm_dir(d: Any) -> str:
    s = str(d or "").strip().upper()
    return s if s in (LONG, SHORT, FLAT) else FLAT


def _extract_json_object(text: str) -> Optional[dict]:
    """Best-effort: pull the first balanced ``{...}`` object out of a reply.

    Tolerates models that wrap JSON in prose or ```` ```json ```` fences.
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 — fall through to brace scan
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start:i + 1]
                    try:
                        return json.loads(chunk)
                    except Exception:  # noqa: BLE001
                        break  # malformed — try the next '{'
        start = text.find("{", start + 1)
    return None


@dataclass
class LLMOpinion:
    """The structured opinion the model returns — emitted as evidence."""

    symbol: str
    direction: str                 # LONG | SHORT | FLAT
    confidence: float              # 0..1
    rationale: str = ""
    competing_hypotheses: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    at_iso: str = ""
    model: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "confidence": round(self.confidence, 4),
            "rationale": self.rationale,
            "competing_hypotheses": list(self.competing_hypotheses),
            "missing_information": list(self.missing_information),
            "at": self.at_iso,
            "model": self.model,
        }

    def as_evidence(self, *, weight: float = 1.0, module: str = "llm_reasoner") -> dict:
        """Vote-like evidence object for the consensus panel (future promotion).

        Shaped like the attributes the panel reads (``module`` / ``direction`` /
        ``confidence`` / ``weight``). It is *evidence*, subject to the same
        performance-based authority and physics vetoes as any module — never an
        override.
        """
        return {
            "module": module,
            "direction": self.direction,
            "confidence": _clamp01(self.confidence),
            "weight": max(0.0, float(weight)),
            "source": "llm",
            "rationale": self.rationale,
        }


class LLMReasoner:
    """Drives the LLM over structured evidence and parses a structured opinion."""

    def __init__(
        self,
        client: Optional[Any] = None,
        *,
        enabled: bool = False,
        drive_decisions: bool = False,
        min_interval_seconds: float = 30.0,
        recent_limit: int = 50,
    ) -> None:
        self._client = client
        self.enabled = bool(enabled)
        self.drive_decisions = bool(drive_decisions)
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._recent_limit = max(1, int(recent_limit))
        self._last_call: dict[str, float] = {}
        self._recent: list[LLMOpinion] = []
        self._calls = 0
        self._faults = 0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        """True when enabled and a usable client is wired."""
        return bool(self.enabled and self._client is not None
                    and getattr(self._client, "usable", False))

    def _throttled(self, key: str, now: float) -> bool:
        if self.min_interval_seconds <= 0:
            return False
        last = self._last_call.get(key, 0.0)
        return (now - last) < self.min_interval_seconds

    def reason(
        self, symbol: str, evidence: dict, *, now: Optional[float] = None,
    ) -> Optional[LLMOpinion]:
        """Ask the model to reason over ``evidence`` for ``symbol``.

        Returns a parsed :class:`LLMOpinion`, or ``None`` when unavailable,
        throttled, or on any fault. Blocking (provider round-trip) — call from a
        background/periodic path, never the hot loop. Fail-safe.
        """
        if not self.available:
            return None
        t = time.time() if now is None else float(now)
        sym = str(symbol or "")
        try:
            with self._lock:
                if self._throttled(sym, t):
                    return None
                self._last_call[sym] = t
            user = self._build_user_prompt(sym, evidence)
            reply = self._client.complete(_SYSTEM_PROMPT, user)
            if not reply:
                with self._lock:
                    self._faults += 1
                return None
            opinion = self._parse(sym, reply)
            with self._lock:
                self._calls += 1
                if opinion is not None:
                    self._recent.append(opinion)
                    if len(self._recent) > self._recent_limit:
                        self._recent = self._recent[-self._recent_limit:]
                else:
                    self._faults += 1
            return opinion
        except Exception as exc:  # noqa: BLE001 — reasoning must never break a cycle
            logger.debug("[llm] reason({}) ignored a fault: {}", symbol, exc)
            with self._lock:
                self._faults += 1
            return None

    @staticmethod
    def _build_user_prompt(symbol: str, evidence: dict) -> str:
        """Serialise the structured evidence into a compact JSON user prompt."""
        payload = {"symbol": symbol, "evidence": evidence or {}}
        try:
            return json.dumps(payload, default=str)[:8000]
        except Exception:  # noqa: BLE001
            return json.dumps({"symbol": symbol})

    def _parse(self, symbol: str, reply: str) -> Optional[LLMOpinion]:
        obj = _extract_json_object(reply)
        if not isinstance(obj, dict):
            return None
        ch = obj.get("competing_hypotheses") or []
        mi = obj.get("missing_information") or []
        return LLMOpinion(
            symbol=symbol,
            direction=_norm_dir(obj.get("direction")),
            confidence=_clamp01(obj.get("confidence")),
            rationale=str(obj.get("rationale", ""))[:500],
            competing_hypotheses=[str(x)[:200] for x in ch][:5]
            if isinstance(ch, list) else [],
            missing_information=[str(x)[:200] for x in mi][:5]
            if isinstance(mi, list) else [],
            at_iso=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            model=str(getattr(self._client, "model", "") or ""),
        )

    def get_status(self) -> dict:
        """Secret-safe status for the dashboard / governance."""
        try:
            with self._lock:
                recent = [o.to_dict() for o in self._recent[-10:]]
                calls, faults = self._calls, self._faults
            client_desc = None
            if self._client is not None and hasattr(self._client, "describe"):
                try:
                    client_desc = self._client.describe()
                except Exception:  # noqa: BLE001
                    client_desc = None
            return {
                "enabled": self.enabled,
                "available": self.available,
                "drive_decisions": self.drive_decisions,
                "min_interval_seconds": self.min_interval_seconds,
                "calls": calls,
                "faults": faults,
                "client": client_desc,
                "recent_opinions": recent,
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[llm] get_status ignored a fault: {}", exc)
            return {"enabled": self.enabled, "available": False}


__all__ = ["LLMReasoner", "LLMOpinion", "LONG", "SHORT", "FLAT"]

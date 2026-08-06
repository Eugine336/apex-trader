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
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from llm.json_repair import repair_json

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"

_SYSTEM_PROMPT = (
    "You are the reasoning subsystem of an autonomous trading intelligence. "
    "You are given STRUCTURED EVIDENCE about one instrument: competing "
    "Long/Short/Flat theses with their expected value and confidence, a summary "
    "of module votes, market regime and structure. You may also receive a live "
    "market picture reconstructed like a trader's screen — multi-timeframe OHLC "
    "candles, current price and spread, the tick tape (velocity, drift, up/down "
    "balance, momentum, spread behaviour), order-book depth when available, the "
    "session, and a pullback read (higher-timeframe trend vs the current lower-"
    "timeframe move). Reason over the evidence — do not invent data you were not "
    "given. Weigh the evidence, consider competing explanations, and state what "
    "would change your mind.\n\n"
    "You are free to exploit the price action you can now see: a pullback within "
    "an uptrend, a bounce within a downtrend, or a short-lived micro-move can be "
    "a valid entry — in either direction — when the read is coherent across the "
    "chart, tape and book. But every action must clear its own cost: only act "
    "when the expected move is worth more than the round-trip spread and "
    "commission. When the edge does not clear costs, or the picture is noise, "
    "answer FLAT. Do not manufacture trades to be busy.\n\n"
    "Judge LONG and SHORT symmetrically. The higher-timeframe trend is CONTEXT, "
    "never a default direction — do not anchor to it. A pullback in an uptrend is "
    "a buy ONLY if the higher timeframe is intact AND the lower timeframe shows "
    "the pullback stabilising or reclaiming (up ticks returning, drift turning "
    "positive, higher low); NEVER buy a market that is still actively falling "
    "(down ticks, negative drift, fresh lower lows) merely because a higher "
    "timeframe is up — that is catching a falling knife. Apply the exact mirror "
    "for shorts. When the freshest lower-timeframe evidence (the tape and the "
    "last candles) conflicts with a stale higher-timeframe lean, trust the fresh "
    "evidence or stay FLAT. For every LONG you consider, state the SHORT case in "
    "competing_hypotheses (and vice-versa); if you cannot, you have not looked. "
    "Prefer FLAT over trading with the crowd's assumed trend.\n\n"
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


_DIR_MAP = {
    "LONG": LONG, "BUY": LONG, "BULLISH": LONG, "UP": LONG,
    "SHORT": SHORT, "SELL": SHORT, "BEARISH": SHORT, "DOWN": SHORT,
    "FLAT": FLAT, "HOLD": FLAT, "NEUTRAL": FLAT, "NONE": FLAT, "WAIT": FLAT,
}
_DIR_RE = re.compile(r"""(?i)["']?\bdirection\b["']?\s*[:=]\s*["']?([A-Za-z]+)""")
_CONF_RE = re.compile(r"""(?i)["']?\bconfidence\b["']?\s*[:=]\s*([0-9]*\.?[0-9]+)""")
_RATIONALE_RE = re.compile(r"""(?i)["']?\brationale\b["']?\s*[:=]\s*["']([^"']*)""")
_TOKEN_RE = re.compile(r"\b(LONG|SHORT|FLAT)\b")


def _map_dir(s: Any) -> str:
    return _DIR_MAP.get(str(s or "").strip().upper(), FLAT)


def _regex_opinion_fields(reply: str) -> Optional[dict]:
    """Last-resort extraction of the opinion fields from unparseable text.

    Pulls ``direction`` / ``confidence`` / ``rationale`` out of a reply that no
    JSON layer could recover (e.g. truncated before the value, or prose). Only
    commits to a direction it can defend: an explicit ``direction:`` field, or —
    failing that — a single unambiguous LONG/SHORT/FLAT token. A directional
    read with no recoverable confidence defaults to 0.0 (weak/observe) rather
    than inventing conviction. Returns ``None`` when no direction is present.
    """
    text = str(reply or "")
    m = _DIR_RE.search(text)
    if m:
        direction = _map_dir(m.group(1))
    else:
        tokens = set(_TOKEN_RE.findall(text))
        if len(tokens) != 1:
            return None
        direction = _map_dir(next(iter(tokens)))
    cm = _CONF_RE.search(text)
    rm = _RATIONALE_RE.search(text)
    return {
        "direction": direction,
        "confidence": cm.group(1) if cm else 0.0,
        "rationale": rm.group(1) if rm else "",
    }


def _extract_opinion_fields(reply: str) -> Optional[dict]:
    """Recover the opinion object from a model reply, robustly (Part XXIII).

    Layered so a fenced/prose-wrapped/truncated reply still yields the Brain's
    decision instead of being discarded: (1) lenient JSON recovery
    (:func:`llm.json_repair.repair_json` — fence strip, strict, balanced scan,
    structural repair of a missing brace / open string / trailing comma); then
    (2) regex field extraction as a final fallback. Never raises.
    """
    try:
        obj = repair_json(reply)
        if isinstance(obj, dict) and "direction" in obj:
            return obj
        if isinstance(obj, list):
            for x in obj:
                if isinstance(x, dict) and "direction" in x:
                    return x
        return _regex_opinion_fields(reply)
    except Exception:  # noqa: BLE001 — recovery must never raise
        return None


def _reply_was_strict_json(reply: str) -> bool:
    """True when the raw reply is already valid JSON with a direction (no repair
    needed) — used only for observability (recovered-vs-clean counting)."""
    try:
        raw = json.loads(str(reply or ""))
        return isinstance(raw, dict) and "direction" in raw
    except Exception:  # noqa: BLE001
        return False


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
        self._recovered = 0   # opinions recovered from non-strict/truncated JSON
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
                    logger.debug(
                        "[llm] {} throttled ({}s min interval) — no fresh model call this cycle",
                        sym, self.min_interval_seconds,
                    )
                    return None
                self._last_call[sym] = t
            user = self._build_user_prompt(sym, evidence)
            reply = self._client.complete(_SYSTEM_PROMPT, user)
            if not reply:
                with self._lock:
                    self._faults += 1
                logger.info("[llm] {} — no reply from model (fail-safe: no opinion)", sym)
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
            if opinion is not None:
                logger.info(
                    "[llm] {} opinion dir={} conf={} — {}",
                    sym,
                    getattr(opinion, "direction", "?"),
                    round(float(getattr(opinion, "confidence", 0.0) or 0.0), 3),
                    str(getattr(opinion, "rationale", "") or "")[:160],
                )
            else:
                # 2xx reply that did not parse into a directional opinion — the
                # single most common "why is the Brain idle?" cause. Surface the
                # raw reply (key-free) so the prompt/format can be corrected.
                logger.warning(
                    "[llm] {} model replied but NO opinion parsed (fail-safe: observe) — reply[:220]={}",
                    sym, str(reply)[:220],
                )
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
            # Larger cap so the reconstructed multi-timeframe price snapshot
            # (Part XIX Art 2 — the chart) reaches the model alongside the
            # analytical reads rather than being truncated away.
            return json.dumps(payload, default=str)[:16000]
        except Exception:  # noqa: BLE001
            return json.dumps({"symbol": symbol})

    def _parse(self, symbol: str, reply: str) -> Optional[LLMOpinion]:
        fields = _extract_opinion_fields(reply)
        if not isinstance(fields, dict) or "direction" not in fields:
            return None
        # Observability: note when the Brain's opinion had to be *recovered* from
        # a fenced / truncated / prose reply rather than clean JSON (Part XXIII).
        if not _reply_was_strict_json(reply):
            with self._lock:
                self._recovered += 1
        ch = fields.get("competing_hypotheses") or []
        mi = fields.get("missing_information") or []
        return LLMOpinion(
            symbol=symbol,
            direction=_norm_dir(fields.get("direction")),
            confidence=_clamp01(fields.get("confidence")),
            rationale=str(fields.get("rationale", ""))[:500],
            competing_hypotheses=[str(x)[:200] for x in ch][:5]
            if isinstance(ch, list) else [],
            missing_information=[str(x)[:200] for x in mi][:5]
            if isinstance(mi, list) else [],
            at_iso=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            model=str(getattr(self._client, "model", "") or ""),
        )

    def latest_opinion(self, symbol: str) -> Optional[LLMOpinion]:
        """Most recent recorded opinion for ``symbol`` (or None). Fail-safe.

        The panel-feeding path (when ``drive_decisions`` is enabled) reads this
        to pull the latest LLM read for a symbol without triggering a call.
        """
        try:
            sym = str(symbol or "")
            with self._lock:
                for op in reversed(self._recent):
                    if op.symbol == sym:
                        return op
            return None
        except Exception:  # noqa: BLE001
            return None

    def get_status(self) -> dict:
        """Secret-safe status for the dashboard / governance."""
        try:
            with self._lock:
                recent = [o.to_dict() for o in self._recent[-10:]]
                calls, faults = self._calls, self._faults
                recovered = self._recovered
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
                "recovered": recovered,
                "client": client_desc,
                "recent_opinions": recent,
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[llm] get_status ignored a fault: {}", exc)
            return {"enabled": self.enabled, "available": False}


__all__ = ["LLMReasoner", "LLMOpinion", "LONG", "SHORT", "FLAT"]

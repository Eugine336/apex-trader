"""APEX TRADER — Evidence adapters (Phase E: full evidence consolidation).

Constitution Part III Art 3 requires the Brain to reason over ALL evidence
domains, not one. These adapters turn already-computed subsystem reads into
structured :class:`~cognition.contracts.Evidence` — read-only, fail-safe, and
carrying no buy/sell instruction (Part III Art 2). Only the Brain synthesises
evidence into a decision.

Per Part XXV, analytical modules are measurement instruments, never voters, and
nothing here returns a directional reading: each contributing module (structure,
liquidity, momentum, volatility, volume, order flow, correlation, session, …) is
surfaced as a domain-classified Evidence that reports an *instrument reading* —
its raw secondary measurements only, with provenance. No observation carries a
direction, lean, vote, probability or LONG/SHORT/FLAT token, and ``polarity`` is
always 0; the Brain alone forms direction from the raw measurements and the live
price picture. This bridges the WorldModel's per-module panel onto the Brain's
MarketState without re-running analysis or needing candle data in the loop.

Pure standard library.
"""

from __future__ import annotations

from typing import Any

from cognition.contracts import Evidence, EvidenceDomain

# Ordered (substring, domain) — most specific first. A module name is classified
# by the first substring it contains. Unmatched names fall back to OTHER.
_DOMAIN_RULES: tuple[tuple[tuple[str, ...], EvidenceDomain], ...] = (
    (("order_block", "orderblock", "fvg", "imbalance", "inducement", "order_flow",
      "orderflow", "delta"), EvidenceDomain.ORDER_FLOW),
    (("liquidity", "sweep", "pool", "grab"), EvidenceDomain.LIQUIDITY),
    (("structure", "wyckoff", "bos", "choch", "market_structure"), EvidenceDomain.STRUCTURE),
    (("momentum", "divergence", "rsi", "macd"), EvidenceDomain.MOMENTUM),
    (("volatility", "atr", "compression", "squeeze"), EvidenceDomain.VOLATILITY),
    (("volume", "vwap", "obv"), EvidenceDomain.VOLUME),
    (("correlation", "currency_strength", "cross_instrument", "dxy"),
     EvidenceDomain.CORRELATION),
    (("session", "killzone", "london", "asia"), EvidenceDomain.SESSION),
    (("news", "macro", "calendar", "cpi", "fomc", "nfp"), EvidenceDomain.MACRO),
    (("htf", "mtf", "timeframe", "alignment", "world_model", "bias"),
     EvidenceDomain.MULTI_TIMEFRAME),
    (("execution", "spread", "slippage", "fill"), EvidenceDomain.EXECUTION_QUALITY),
    (("portfolio", "exposure", "heat"), EvidenceDomain.PORTFOLIO),
    (("analogue", "historical", "instrument_stats", "stats"),
     EvidenceDomain.HISTORICAL_ANALOGUE),
    (("reasoning_engine", "reasoner", "llm"), EvidenceDomain.REASONING),
)


def classify_domain(module_name: Any) -> EvidenceDomain:
    """Map an analytical module's name to its evidence domain (fail-safe)."""
    name = str(module_name or "").strip().lower()
    if not name:
        return EvidenceDomain.OTHER
    for substrings, domain in _DOMAIN_RULES:
        for s in substrings:
            if s in name:
                return domain
    return EvidenceDomain.OTHER


def _clamp01(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return min(1.0, max(0.0, f))


def evidence_from_thesis_status(status: dict, symbol: str) -> "list[Evidence]":
    """Convert one symbol's ThesisEngine status into structured Evidence.

    Emits an aggregate multi-timeframe read plus one domain-classified Evidence
    per contributing analytical module, with no directional reading. Fail-safe:
    returns what it can and never raises.

    Part XXV — the Brain must never receive a collapsed LONG/SHORT/FLAT verdict.
    This adapter therefore never inspects the thesis's ``dominant`` direction and
    never splits modules into supporting vs opposing around it (both are a
    pre-reasoning directional collapse). It surfaces the panel's overall
    conviction *magnitude* and every contributing module once, as equal
    instrument readings; the Brain alone forms direction from them.
    """
    out: list[Evidence] = []
    try:
        theses = (status or {}).get("theses") or {}
        entry = theses.get(symbol)
        if not isinstance(entry, dict):
            return out

        # Direction-agnostic overall conviction: the strongest side's confidence
        # is a magnitude of how convicted the panel is, never a chosen direction.
        side_confs = [
            _clamp01((entry.get(side) or {}).get("confidence", 0.0))
            for side in ("long", "short", "flat")
        ]
        agg_conf = max(side_confs) if side_confs else 0.0

        out.append(Evidence(
            source_module="brain.thesis_engine",
            domain=EvidenceDomain.MULTI_TIMEFRAME, symbol=symbol,
            observation="multi-timeframe instrument reading",
            confidence=agg_conf, uncertainty=1.0 - agg_conf, polarity=0.0,
            relevance_horizon_seconds=900.0,
        ))

        # Every contributing module (across every side) is an equal, non-
        # directional instrument reading. Each module's confidence is the
        # strongest reading it carries on any side — never a supporting/opposing
        # bucket chosen by a dominant direction. Deduped, first-seen order.
        module_conf: "dict[str, float]" = {}
        for side in ("long", "short", "flat"):
            bucket = entry.get(side) or {}
            side_conf = _clamp01(bucket.get("confidence", 0.0))
            for key in ("supporting_modules", "opposing_modules"):
                for m in (bucket.get(key) or []):
                    name = str(m)
                    if name:
                        module_conf[name] = max(module_conf.get(name, 0.0), side_conf)
        for name, conf in module_conf.items():
            out.append(Evidence(
                source_module=name, domain=classify_domain(name), symbol=symbol,
                observation=f"{name} instrument reading",
                confidence=conf, uncertainty=1.0 - conf,
                polarity=0.0, relevance_horizon_seconds=900.0,
            ))
    except Exception:  # noqa: BLE001 — consolidation must never raise
        return out
    return out


def _vote_measurements(vote: Any, weight: Any, timeframe: str) -> dict:
    """Flatten a vote's rich per-module ``evidence`` dict into JSON-safe
    measurements.

    The ``(direction, confidence)`` collapse used to discard every secondary
    read a module computed (RSI level, MACD histogram, zone stacking, sweep
    type, …). ``Vote.evidence`` preserves it; this surfaces it to the Brain as
    structured measurements so the reasoner sees the full per-module picture.
    Only scalar values are carried (numbers, bools, short strings) so the LLM
    payload stays clean and serialisable. No directional field is emitted (Part
    XXV); any directional key that slips through is stripped when the Evidence
    enters the MarketState (see ``contracts.scrub_directional``).
    """
    out: dict = {"weight": _clamp01(weight, 1.0)}
    if timeframe:
        out["timeframe"] = timeframe
    try:
        raw = getattr(vote, "evidence", None)
        if isinstance(raw, dict):
            for k, val in raw.items():
                if len(out) >= 24:
                    break
                key = str(k)[:48]
                if key in out:
                    continue
                if isinstance(val, bool) or isinstance(val, (int, float)):
                    out[key] = val
                elif isinstance(val, str):
                    out[key] = val[:120]
    except Exception:  # noqa: BLE001
        pass
    return out


def evidence_from_votes(symbol: str, votes: Any) -> "list[Evidence]":
    """Convert a raw vote panel (module/direction/confidence/weight) to Evidence.

    Each contributing analytical module (structure, liquidity, momentum, volume,
    order-flow, …) becomes one domain-classified :class:`Evidence`, carrying the
    module's richer secondary read (``Vote.evidence``) as measurements so nothing
    is collapsed away. This is the live bridge from the WorldModel's per-module
    vote panel onto the Brain's consolidated MarketState. Fail-safe.
    """
    out: list[Evidence] = []
    try:
        for v in list(votes or []):
            module = str(getattr(v, "module", "") or getattr(v, "source", "") or "")
            if not module:
                continue
            conf = _clamp01(getattr(v, "confidence", 0.0))
            weight = getattr(v, "weight", 1.0)
            timeframe = str(getattr(v, "timeframe", "") or "")
            observation = f"{module} instrument reading"
            if timeframe:
                observation += f" on {timeframe}"
            out.append(Evidence(
                source_module=module, domain=classify_domain(module), symbol=symbol,
                observation=observation,
                confidence=conf, uncertainty=1.0 - conf,
                polarity=0.0,
                measurements=_vote_measurements(v, weight, timeframe),
                relevance_horizon_seconds=900.0,
            ))
    except Exception:  # noqa: BLE001
        return out
    return out


def _num(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def evidence_from_developing_bias(symbol: str, bias: Any) -> "list[Evidence]":
    """Convert the DEVELOPING (forming-bar) WorldModel bias into one Evidence.

    Closed-candle votes are the confirmed read; the developing store carries the
    *fresher* directional synthesis computed on the still-forming bar (no votes,
    only a bias dict). Surfacing it as a single, clearly-labelled multi-timeframe
    Evidence lets the Brain react between candle closes without mistaking it for
    a confirmed module vote. It is deliberately short-lived (a forming bar's read
    goes stale in seconds) and directionally attenuated so it informs rather than
    dominates the confirmed panel. Fail-safe: returns ``[]`` on empty/any fault.
    """
    out: list[Evidence] = []
    try:
        b = dict(bias or {})
        if not b:
            return out
        # Prefer the explicit confidence; fall back to the dominant probability
        # as a *magnitude of conviction* (never a direction).
        conf = _clamp01(b.get("confidence", 0.0))
        if conf <= 0.0:
            conf = _clamp01(max(_num(b.get("long_probability")),
                               _num(b.get("short_probability"))))
        conflict = _clamp01(b.get("conflict_score", 0.0))
        observation = (
            f"developing-candle instrument reading "
            f"(reading strength {conf:.2f}, conflict {conflict:.2f})"
        )
        out.append(Evidence(
            source_module="world_model.developing",
            domain=EvidenceDomain.MULTI_TIMEFRAME, symbol=symbol,
            observation=observation,
            confidence=conf,
            # Higher forming-bar conflict ⇒ more source-side doubt.
            uncertainty=_clamp01(max(1.0 - conf, conflict)),
            polarity=0.0,
            measurements={
                "developing": True,
                "conflict_score": round(conflict, 4),
                "strength": str(b.get("strength", "") or "")[:32],
                "tradeable": bool(b.get("tradeable", False)),
            },
            relevance_horizon_seconds=120.0,
        ))
    except Exception:  # noqa: BLE001
        return out
    return out


def _text(item: dict, *keys: str, limit: int = 200) -> str:
    for k in keys:
        v = item.get(k)
        if v:
            return str(v)[:limit]
    return ""


def evidence_from_knowledge(
    symbol: str,
    payload: Any,
    *,
    source: str = "composio.knowledge",
    max_items: int = 5,
) -> "list[Evidence]":
    """Convert a Composio READ payload into advisory Evidence (Part IX v3.0).

    Handles two shapes returned by the knowledge layer:

    * **Research / market context** — ``{"items": [{title, snippet, source,
      sentiment, ...}]}`` (or a bare list). Each item becomes one MACRO-domain
      Evidence: external context, directionless unless the provider states a
      sentiment/direction, and modestly confident. This is *context*, never a
      trade signal.
    * **Advisor answer** — ``{"answer": ..., "direction": ..., "confidence":
      ...}`` from an AI advisor reached through Composio. Becomes one REASONING
      Evidence (Article 8 — advisory, never a second Brain).

    Bounded (``max_items``), string-capped and fully fail-safe (``[]`` on any
    fault) so an untrusted external payload can never break consolidation.
    """
    out: list[Evidence] = []
    try:
        data = payload if isinstance(payload, dict) else {"items": payload}
        # ── Advisor answer → one REASONING evidence ───────────────────────
        answer = _text(data, "answer", "summary", "content", limit=400)
        if answer:
            conf = _clamp01(data.get("confidence", 0.4))
            advisor = str(data.get("advisor", data.get("source", "")) or "advisor")[:48]
            out.append(Evidence(
                source_module=f"{source}.{advisor}"[:80],
                domain=EvidenceDomain.REASONING, symbol=symbol,
                observation=f"advisor {advisor}: {answer}",
                confidence=conf, uncertainty=1.0 - conf,
                polarity=0.0,
                measurements={"advisor": True},
                relevance_horizon_seconds=1800.0,
            ))
        # ── Research / market-context items → MACRO evidence each ─────────
        items = data.get("items", data.get("results", []))
        if isinstance(items, dict):
            items = [items]
        for item in list(items or [])[: max(1, int(max_items))]:
            if not isinstance(item, dict):
                continue
            title = _text(item, "title", "headline", "name")
            snippet = _text(item, "snippet", "summary", "description", "content")
            if not title and not snippet:
                continue
            conf = _clamp01(item.get("confidence", 0.35))
            out.append(Evidence(
                source_module=source,
                domain=EvidenceDomain.MACRO, symbol=symbol,
                observation=(f"{title}: {snippet}" if title and snippet
                             else (title or snippet))[:280],
                confidence=conf, uncertainty=1.0 - conf,
                polarity=0.0,
                measurements={
                    "provider": str(item.get("source", "") or "")[:48],
                    "external": True,
                },
                relevance_horizon_seconds=1800.0,
            ))
    except Exception:  # noqa: BLE001
        return out
    return out


def evidence_from_analogues(symbol: str, analogues: Any) -> "list[Evidence]":
    """Summarise similar past campaigns (institutional memory) as one Evidence.

    Part VII: the Brain consults history. Each analogue carries a similarity, a
    held ``direction``, a realised ``outcome_won`` and a ``reasoning_quality``.
    A won campaign in a direction is *supporting* evidence for that direction; a
    lost one is *contradicting* (that setup previously failed). Contributions are
    weighted by similarity × reasoning_quality and summed into a single bounded
    ``historical_analogue`` Evidence. Never a signal — just the memory's lean.
    Fail-safe: returns ``[]`` on empty input or any fault.
    """
    out: list[Evidence] = []
    try:
        items = [a for a in list(analogues or []) if isinstance(a, dict)]
        if not items:
            return out
        wins = 0
        losses = 0
        for a in items:
            sim = _clamp01(a.get("similarity", 0.0))
            if sim <= 0.0:
                continue
            won = bool(a.get("outcome_won", False))
            wins += 1 if won else 0
            losses += 0 if won else 1
        n = wins + losses
        if n == 0:
            return out
        mean_sim = sum(_clamp01(a.get("similarity", 0.0)) for a in items) / len(items)
        # Confidence rises with how similar and how numerous the analogues are.
        confidence = _clamp01(mean_sim * min(1.0, n / 5.0))
        out.append(Evidence(
            source_module="cognition.memory.analogues",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE, symbol=str(symbol or ""),
            observation=(f"{n} analogous past campaigns: {wins} won / {losses} lost "
                         f"(avg similarity {mean_sim:.2f})"),
            confidence=confidence, uncertainty=1.0 - confidence, polarity=0.0,
            measurements={"analogues": n, "wins": wins, "losses": losses,
                          "mean_similarity": round(mean_sim, 4)},
            relevance_horizon_seconds=1800.0,
        ))
    except Exception:  # noqa: BLE001 — consolidation must never raise
        return out
    return out


__all__ = [
    "classify_domain",
    "evidence_from_thesis_status",
    "evidence_from_votes",
    "evidence_from_analogues",
    "evidence_from_reasoning",
]


def evidence_from_reasoning(symbol: str, consultation: Any) -> "list[Evidence]":
    """Turn a multi-engine reasoning consultation into per-engine Evidence.

    Part XVII Art 7: every external opinion is *evidence, not truth* — so each
    engine's opinion becomes its own :class:`~cognition.contracts.Evidence`
    (``source_module="reasoning_engine.<name>"``, domain ``REASONING``), never a
    vote or an average. The Brain synthesises them alongside all other evidence,
    and the Phase VIII influence ledger grades each engine by realised outcome
    (Art 11). Fail-safe: returns ``[]`` on empty input or any fault.
    """
    out: list[Evidence] = []
    try:
        opinions = list(getattr(consultation, "opinions", None) or [])
        for op in opinions:
            engine = str(getattr(op, "engine", "") or "").strip()
            if not engine:
                continue
            conf = _clamp01(getattr(op, "confidence", 0.0))
            rationale = str(getattr(op, "rationale", "") or "")
            out.append(Evidence(
                source_module=f"reasoning_engine.{engine}",
                domain=EvidenceDomain.REASONING, symbol=str(symbol or ""),
                observation=f"{engine} reasons: {rationale[:200]}",
                confidence=conf, uncertainty=1.0 - conf,
                polarity=0.0,
                measurements={"engine": engine,
                              "latency_ms": round(float(getattr(op, "latency_ms", 0.0) or 0.0), 1)},
                relevance_horizon_seconds=900.0,
            ))
    except Exception:  # noqa: BLE001 — consolidation must never raise
        return out
    return out


def _components(symbol: str) -> "set[str]":
    """Split a symbol into correlated legs (mirrors brain.campaign). Local to
    avoid a cognition→brain import; kept tiny and pure."""
    s = str(symbol or "").upper().strip()
    if len(s) == 6 and s.isalpha():
        return {s[:3], s[3:]}
    return {s} if s else set()


def evidence_from_portfolio(symbol: str, assessment: Any) -> "list[Evidence]":
    """Turn the live portfolio assessment into portfolio-context Evidence.

    Part XVIII Art 11: the Brain reasons across the whole book. When it reasons
    one symbol it must see how that symbol's exposure stacks against the rest —
    correlated clusters sharing a currency/asset leg, and overall concentration.
    This is a *risk context*, not a directional signal, so polarity is 0; the
    Brain decides whether concentration argues against adding exposure. Only the
    correlated cluster(s) that share a leg with ``symbol`` (plus a book-wide
    concentration note) are surfaced. Fail-safe: ``[]`` on empty/any fault.
    """
    out: list[Evidence] = []
    try:
        a = dict(assessment or {})
        n = int(a.get("campaign_count", 0) or 0)
        if n < 2:
            return out
        sym = str(symbol or "").upper()
        my_comps = _components(sym)
        clusters = [c for c in (a.get("correlated_clusters") or []) if isinstance(c, dict)]
        relevant = [c for c in clusters if str(c.get("component", "")) in my_comps]
        concentration = _clamp01(a.get("concentration", 0.0))
        for c in relevant:
            comp = str(c.get("component", ""))
            members = [m for m in (c.get("members") or []) if isinstance(m, dict)]
            peers = [
                f"{m.get('symbol')}:{m.get('direction')}" for m in members
                if str(m.get("symbol", "")).upper() != sym
            ]
            share = _clamp01(len(members) / float(n)) if n else 0.0
            out.append(Evidence(
                source_module="portfolio.correlation",
                domain=EvidenceDomain.PORTFOLIO, symbol=sym,
                observation=(
                    f"{len(members)} live campaign(s) share '{comp}' exposure "
                    f"({', '.join(peers) or 'this symbol'}) — correlated/"
                    "concentrated book risk"
                ),
                confidence=max(share, concentration),
                uncertainty=1.0 - max(share, concentration),
                polarity=0.0,  # risk context, never a directional instruction
                measurements={
                    "component": comp,
                    "cluster_count": int(c.get("count", len(members))),
                    "concentration": round(concentration, 4),
                    "campaign_count": n,
                },
                relevance_horizon_seconds=300.0,
            ))
        if not relevant and bool(a.get("concentration_warning", False)):
            out.append(Evidence(
                source_module="portfolio.concentration",
                domain=EvidenceDomain.PORTFOLIO, symbol=sym,
                observation=(
                    f"book concentration {concentration:.0%} across {n} campaigns "
                    "— limited diversification"
                ),
                confidence=concentration, uncertainty=1.0 - concentration,
                polarity=0.0,
                measurements={"concentration": round(concentration, 4),
                              "campaign_count": n},
                relevance_horizon_seconds=300.0,
            ))
    except Exception:  # noqa: BLE001 — consolidation must never raise
        return out
    return out

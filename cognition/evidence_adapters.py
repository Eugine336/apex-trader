"""APEX TRADER — Evidence adapters (Phase E: full evidence consolidation).

Constitution Part III Art 3 requires the Brain to reason over ALL evidence
domains, not one. These adapters turn already-computed subsystem reads into
structured :class:`~cognition.contracts.Evidence` — read-only, fail-safe, and
carrying no buy/sell instruction (Part III Art 2). Only the Brain synthesises
evidence into a decision.

The richest already-available source is the vote panel the legacy consensus
computes each cycle: every contributing analytical module (structure, liquidity,
momentum, volatility, volume, order flow, correlation, session, …) appears as a
supporting/opposing module on the ThesisEngine's per-symbol theses. We convert
each into a domain-classified Evidence with provenance, so the Brain sees the
full picture without re-running analysis or needing candle data in the loop.

Pure standard library.
"""

from __future__ import annotations

from typing import Any, Optional

from cognition.contracts import Evidence, EvidenceDomain

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"

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


def _sign(direction: str) -> float:
    d = str(direction or "").upper()
    return 1.0 if d == LONG else (-1.0 if d == SHORT else 0.0)


def evidence_from_thesis_status(status: dict, symbol: str) -> "list[Evidence]":
    """Convert one symbol's ThesisEngine status into structured Evidence.

    Emits an aggregate multi-timeframe read plus one domain-classified Evidence
    per supporting/opposing module of the dominant thesis. Fail-safe: returns
    what it can and never raises.
    """
    out: list[Evidence] = []
    try:
        theses = (status or {}).get("theses") or {}
        entry = theses.get(symbol)
        if not isinstance(entry, dict):
            return out
        eff = entry.get("effective") or {}
        dominant = str(eff.get("dominant", "FLAT") or "FLAT").upper()
        long_ev = float(eff.get("long_ev", 0.0) or 0.0)
        short_ev = float(eff.get("short_ev", 0.0) or 0.0)
        flat_ev = float(eff.get("flat_ev", 0.0) or 0.0)

        if dominant == LONG:
            agg_pol = min(1.0, max(0.0, long_ev - flat_ev))
            agg_conf = _clamp01((entry.get("long") or {}).get("confidence", 0.0))
        elif dominant == SHORT:
            agg_pol = -min(1.0, max(0.0, short_ev - flat_ev))
            agg_conf = _clamp01((entry.get("short") or {}).get("confidence", 0.0))
        else:
            agg_pol = 0.0
            agg_conf = _clamp01((entry.get("flat") or {}).get("confidence", 0.0))

        out.append(Evidence(
            source_module="brain.thesis_engine",
            domain=EvidenceDomain.MULTI_TIMEFRAME, symbol=symbol,
            observation=f"dominant competing thesis {dominant}",
            confidence=agg_conf, uncertainty=1.0 - agg_conf, polarity=agg_pol,
            measurements={"long_ev": long_ev, "short_ev": short_ev,
                          "flat_ev": flat_ev, "dominant": dominant},
            relevance_horizon_seconds=900.0,
        ))

        # Per-module evidence from the dominant directional thesis.
        if dominant in (LONG, SHORT):
            sign = _sign(dominant)
            dom = entry.get("long" if dominant == LONG else "short") or {}
            opp = entry.get("short" if dominant == LONG else "long") or {}
            sup_conf = _clamp01(dom.get("confidence", 0.0))
            opp_conf = _clamp01(opp.get("confidence", 0.0)) or 0.4
            for m in (dom.get("supporting_modules") or []):
                out.append(Evidence(
                    source_module=str(m), domain=classify_domain(m), symbol=symbol,
                    observation=f"{m} supports {dominant}",
                    confidence=sup_conf, uncertainty=1.0 - sup_conf,
                    polarity=sign * sup_conf, relevance_horizon_seconds=900.0,
                ))
            for m in (dom.get("opposing_modules") or []):
                out.append(Evidence(
                    source_module=str(m), domain=classify_domain(m), symbol=symbol,
                    observation=f"{m} opposes {dominant}",
                    confidence=opp_conf, uncertainty=1.0 - opp_conf,
                    polarity=-sign * opp_conf, relevance_horizon_seconds=900.0,
                ))
    except Exception:  # noqa: BLE001 — consolidation must never raise
        return out
    return out


def evidence_from_votes(symbol: str, votes: Any) -> "list[Evidence]":
    """Convert a raw vote panel (module/direction/confidence/weight) to Evidence.

    Available for the fuller wiring once the loop can supply the WorldModel vote
    panel directly; each vote becomes one domain-classified Evidence. Fail-safe.
    """
    out: list[Evidence] = []
    try:
        for v in list(votes or []):
            module = str(getattr(v, "module", "") or getattr(v, "source", "") or "")
            if not module:
                continue
            direction = getattr(v, "direction", "")
            conf = _clamp01(getattr(v, "confidence", 0.0))
            weight = getattr(v, "weight", 1.0)
            out.append(Evidence(
                source_module=module, domain=classify_domain(module), symbol=symbol,
                observation=f"{module} votes {str(direction or '').upper() or 'FLAT'}",
                confidence=conf, uncertainty=1.0 - conf,
                polarity=_sign(direction) * conf,
                measurements={"weight": _clamp01(weight, 1.0)},
                relevance_horizon_seconds=900.0,
            ))
    except Exception:  # noqa: BLE001
        return out
    return out


__all__ = ["classify_domain", "evidence_from_thesis_status", "evidence_from_votes"]

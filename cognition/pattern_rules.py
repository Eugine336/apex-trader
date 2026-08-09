"""APEX TRADER — Deterministic structural pattern rules for the Brain.

Constitution Article X mandates that APEX "must be capable of discovering
relationships, patterns, causal sequences, unusual market behaviour, temporary
asymmetries, interactions between otherwise independent observations". The
:class:`~cognition.brain_reasoning.BrainReasoner` uses these rules to read the
consolidated :class:`~cognition.contracts.Evidence` and surface the structural
relationships a human trader's eyes would see scanning their screens — WITHOUT
any LLM call.

Each rule is a pure function ``rule(evidence: list[Evidence]) -> Optional[str]``
that returns a ``"pattern_name: human description"`` string when the pattern is
present across the evidence, or ``None`` when it is not. This is DETERMINISTIC
pattern matching on structured measurements — not AI inference. Rules are
fail-safe (a malformed Evidence never raises) and never emit a directional
buy/sell instruction; they describe a structural relationship only. The Brain
alone forms direction and hypotheses from the detected patterns.

Pure standard library so the rules are trivially importable and testable.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from cognition.contracts import Evidence, EvidenceDomain

# ── low-level, fail-safe evidence readers ─────────────────────────────────────


def _measurements(ev: Any) -> dict:
    m = getattr(ev, "measurements", None)
    return m if isinstance(m, dict) else {}


def _observation(ev: Any) -> str:
    try:
        return str(getattr(ev, "observation", "") or "").lower()
    except Exception:  # noqa: BLE001 — a reader must never raise
        return ""


def in_domain(evidence: "list[Evidence]", domain: EvidenceDomain) -> "list[Evidence]":
    """All evidence items classified in ``domain`` (fail-safe)."""
    out: list[Evidence] = []
    for e in evidence or []:
        if getattr(e, "domain", None) == domain:
            out.append(e)
    return out


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def has_flag(ev_list: "list[Evidence]", *keys: str) -> bool:
    """True when any evidence carries any of ``keys`` as a truthy measurement."""
    for e in ev_list:
        m = _measurements(e)
        for k in keys:
            if k in m and bool(m.get(k)):
                return True
    return False


def numeric(ev_list: "list[Evidence]", key: str) -> Optional[float]:
    """The first parseable numeric value of ``key`` across ``ev_list``."""
    for e in ev_list:
        v = _num(_measurements(e).get(key))
        if v is not None:
            return v
    return None


def state_is(ev_list: "list[Evidence]", key: str, *values: str) -> bool:
    """True when any evidence's ``key`` measurement matches one of ``values``."""
    wanted = {v.strip().lower() for v in values}
    for e in ev_list:
        raw = _measurements(e).get(key)
        if raw is not None and str(raw).strip().lower() in wanted:
            return True
    return False


def obs_has(ev_list: "list[Evidence]", *subs: str) -> bool:
    """True when any evidence's observation text contains any of ``subs``."""
    lowered = [s.lower() for s in subs]
    for e in ev_list:
        text = _observation(e)
        if text and any(s in text for s in lowered):
            return True
    return False


# ── pattern rules (Article X — structural relationship discovery) ─────────────


def rule_liquidity_sweep_absorbed(evidence: "list[Evidence]") -> Optional[str]:
    liq = in_domain(evidence, EvidenceDomain.LIQUIDITY)
    swept = has_flag(liq, "sweep", "sweep_type", "pool") or obs_has(liq, "sweep", "grab")
    absorbed = has_flag(liq, "absorption", "absorbed") or obs_has(
        liq, "absorb", "absorption")
    if swept and absorbed:
        return ("liquidity_sweep_absorbed: side liquidity taken and the resulting "
                "flow was absorbed — potential reversal setup")
    return None


def rule_liquidity_sweep_momentum_exhaustion(evidence: "list[Evidence]") -> Optional[str]:
    liq = in_domain(evidence, EvidenceDomain.LIQUIDITY)
    mom = in_domain(evidence, EvidenceDomain.MOMENTUM)
    swept = has_flag(liq, "sweep", "sweep_type") or obs_has(liq, "sweep", "grab")
    exhausted = (has_flag(mom, "deceleration", "exhaustion")
                 or state_is(mom, "momentum_state", "decelerating", "exhausting")
                 or obs_has(mom, "decel", "exhaust"))
    if swept and exhausted:
        return ("liquidity_sweep_with_momentum_exhaustion: liquidity grab into "
                "decelerating momentum — the drive that took the liquidity is fading")
    return None


def rule_displacement_confirmed(evidence: "list[Evidence]") -> Optional[str]:
    struct = in_domain(evidence, EvidenceDomain.STRUCTURE)
    vol = in_domain(evidence, EvidenceDomain.VOLUME)
    displaced = (has_flag(struct, "displacement", "bos")
                 or (numeric(struct, "displacement") or 0.0) != 0.0
                 or obs_has(struct, "displacement"))
    volume_up = (has_flag(vol, "expansion", "volume_expansion")
                 or state_is(vol, "volume_state", "above_average", "expanding")
                 or (numeric(vol, "volume_z") or 0.0) >= 1.0
                 or obs_has(vol, "above average", "expansion"))
    if displaced and volume_up:
        return ("displacement_confirmed: structural displacement backed by volume "
                "expansion — genuine directional intent, trend continuation")
    return None


def rule_momentum_divergence(evidence: "list[Evidence]") -> Optional[str]:
    mom = in_domain(evidence, EvidenceDomain.MOMENTUM)
    if has_flag(mom, "divergence") or obs_has(mom, "divergence", "diverging"):
        return ("momentum_divergence: price advancing but momentum weakening — "
                "thesis exhaustion risk")
    return None


def rule_multi_tf_aligned(evidence: "list[Evidence]") -> Optional[str]:
    mtf = in_domain(evidence, EvidenceDomain.MULTI_TIMEFRAME)
    aligned = (has_flag(mtf, "alignment_agree", "aligned")
               or state_is(mtf, "alignment", "aligned", "agree")
               or obs_has(mtf, "aligned"))
    low_conflict = (numeric(mtf, "conflict_score") or 1.0) <= 0.2
    if mtf and (aligned or (len(mtf) >= 2 and low_conflict)):
        return ("multi_tf_aligned: higher and lower timeframe readings agree — "
                "higher-conviction setup")
    return None


def rule_multi_tf_conflict(evidence: "list[Evidence]") -> Optional[str]:
    mtf = in_domain(evidence, EvidenceDomain.MULTI_TIMEFRAME)
    conflicted = (has_flag(mtf, "conflict")
                  or state_is(mtf, "alignment", "conflict", "conflicted")
                  or (numeric(mtf, "conflict_score") or 0.0) >= 0.5
                  or obs_has(mtf, "conflict", "disagree"))
    if conflicted:
        return ("multi_tf_conflict: higher and lower timeframe readings disagree — "
                "uncertainty elevated")
    return None


def rule_volatility_compression(evidence: "list[Evidence]") -> Optional[str]:
    vlt = in_domain(evidence, EvidenceDomain.VOLATILITY)
    compressed = (has_flag(vlt, "compression", "squeeze")
                  or state_is(vlt, "volatility_state", "compressing", "contracting")
                  or obs_has(vlt, "compression", "squeeze", "contracting"))
    if compressed:
        return ("volatility_compression: volatility contracting after expansion — "
                "potential breakout pending")
    return None


def rule_volume_confirmed_breakout(evidence: "list[Evidence]") -> Optional[str]:
    vol = in_domain(evidence, EvidenceDomain.VOLUME)
    vlt = in_domain(evidence, EvidenceDomain.VOLATILITY)
    volume_up = (has_flag(vol, "expansion", "volume_expansion")
                 or state_is(vol, "volume_state", "above_average", "expanding")
                 or (numeric(vol, "volume_z") or 0.0) >= 1.0)
    vol_expanding = (has_flag(vlt, "expansion")
                     or state_is(vlt, "volatility_state", "expanding")
                     or obs_has(vlt, "expansion", "expanding"))
    if volume_up and vol_expanding:
        return ("volume_confirmed_breakout: volume and volatility expanding "
                "together — breakout has participation behind it")
    return None


def rule_order_flow_at_structure(evidence: "list[Evidence]") -> Optional[str]:
    flow = in_domain(evidence, EvidenceDomain.ORDER_FLOW)
    struct = in_domain(evidence, EvidenceDomain.STRUCTURE)
    imbalance = (has_flag(flow, "imbalance")
                 or (numeric(flow, "delta") or 0.0) != 0.0
                 or obs_has(flow, "imbalance", "aggressive"))
    at_level = (has_flag(struct, "support", "resistance", "level")
                or (numeric(struct, "structural_integrity") or 0.0) >= 0.5
                or obs_has(struct, "support", "resistance", "level"))
    if imbalance and at_level:
        return ("order_flow_at_structure: aggressive order flow arriving at a "
                "structural level — flow meeting structure")
    return None


def rule_fvg_liquidity_confluence(evidence: "list[Evidence]") -> Optional[str]:
    flow = in_domain(evidence, EvidenceDomain.ORDER_FLOW)
    liq = in_domain(evidence, EvidenceDomain.LIQUIDITY)
    fvg = has_flag(flow, "fvg", "fair_value_gap", "imbalance_gap") or obs_has(flow, "fvg", "fair value gap")
    pool = has_flag(liq, "pool", "liquidity_pool") or obs_has(liq, "pool")
    if fvg and pool:
        return ("fvg_liquidity_confluence: fair value gap coincides with a liquidity "
                "pool — high-probability reaction zone")
    return None


def rule_structural_break(evidence: "list[Evidence]") -> Optional[str]:
    struct = in_domain(evidence, EvidenceDomain.STRUCTURE)
    if has_flag(struct, "bos", "choch") or obs_has(struct, "break of structure", "change of character"):
        return ("structural_break: break of structure / change of character "
                "detected — the prevailing structure has shifted")
    return None


def rule_momentum_acceleration(evidence: "list[Evidence]") -> Optional[str]:
    mom = in_domain(evidence, EvidenceDomain.MOMENTUM)
    if (has_flag(mom, "acceleration")
            or state_is(mom, "momentum_state", "accelerating")
            or obs_has(mom, "accelerating")):
        return ("momentum_acceleration: momentum accelerating in the direction of "
                "the move — continuation pressure building")
    return None


def rule_execution_degraded(evidence: "list[Evidence]") -> Optional[str]:
    exq = in_domain(evidence, EvidenceDomain.EXECUTION_QUALITY)
    widening = (has_flag(exq, "spread_widening")
                or state_is(exq, "spread_state", "widening")
                or (numeric(exq, "estimated_total_cost_r") or 0.0) >= 1.0
                or obs_has(exq, "spread widening", "widening"))
    if widening:
        return ("execution_degraded: spread widening beyond typical — execution "
                "quality reduced")
    return None


def rule_portfolio_concentrated(evidence: "list[Evidence]") -> Optional[str]:
    port = in_domain(evidence, EvidenceDomain.PORTFOLIO)
    concentrated = (has_flag(port, "concentration_warning")
                    or (numeric(port, "concentration") or 0.0) >= 0.5
                    or obs_has(port, "concentrat"))
    if concentrated:
        return ("portfolio_concentrated: correlated exposure elevated — a new "
                "position increases concentration risk")
    return None


def rule_order_flow_imbalance(evidence: "list[Evidence]") -> Optional[str]:
    flow = in_domain(evidence, EvidenceDomain.ORDER_FLOW)
    delta = numeric(flow, "delta")
    strong = (has_flag(flow, "strong_imbalance")
              or (delta is not None and abs(delta) >= 0.6)
              or (numeric(flow, "imbalance") or 0.0) >= 0.6)
    if strong:
        return ("order_flow_imbalance: pronounced order-flow imbalance — one side "
                "is being aggressively lifted/hit")
    return None


# Ordered registry of every rule (Article X). Extend by appending a new rule
# function; the Brain iterates this list to build its detected-pattern set.
_PATTERN_RULES: "list[Callable[[list[Evidence]], Optional[str]]]" = [
    rule_liquidity_sweep_absorbed,
    rule_liquidity_sweep_momentum_exhaustion,
    rule_displacement_confirmed,
    rule_momentum_divergence,
    rule_multi_tf_aligned,
    rule_multi_tf_conflict,
    rule_volatility_compression,
    rule_volume_confirmed_breakout,
    rule_order_flow_at_structure,
    rule_fvg_liquidity_confluence,
    rule_structural_break,
    rule_momentum_acceleration,
    rule_execution_degraded,
    rule_portfolio_concentrated,
    rule_order_flow_imbalance,
]


def detect_patterns(evidence: "list[Evidence]") -> "list[str]":
    """Run every rule over ``evidence`` and return the detected pattern strings.

    Fail-safe: a rule that raises is skipped, never breaking the reasoning pass.
    """
    found: list[str] = []
    for rule in _PATTERN_RULES:
        try:
            hit = rule(evidence)
        except Exception:  # noqa: BLE001 — one rule must never break the rest
            hit = None
        if hit:
            found.append(hit)
    return found


def pattern_name(pattern: str) -> str:
    """The short name from a ``"name: description"`` pattern string."""
    return str(pattern or "").split(":", 1)[0].strip()


__all__ = [
    "detect_patterns",
    "pattern_name",
    "in_domain",
    "has_flag",
    "numeric",
    "state_is",
    "obs_has",
    "_PATTERN_RULES",
]

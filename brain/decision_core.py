"""APEX TRADER — Shared ED Decision Core.

The single implementation of the event-driven analysis pipeline, factored out of
``scanner.candle_close_handler`` so that **both** the live handler and the
backtest engine decide through identical code (no live/backtest drift).

It is pure over candle data — no EventBus, no WorldModelStore, no threads:

  * :func:`run_tf_modules`  — run the brain modules for one timeframe.
  * :func:`compute_bias`    — synthesize the directional bias from structure.
  * :func:`blend_concepts`  — fold non-ICT concepts into the bias (learned edge).
  * :func:`build_consensus` — per-module votes + ranked opportunity candidates.
  * :func:`fvg_proximity`   — instrument FVG proximity (price units).
  * :func:`analyze_window`  — one-shot: a full multi-TF candle set → WorldModel.

The live ``CandleCloseHandler`` delegates its per-candle analysis to these
functions; :func:`analyze_window` composes them for a complete window so the
backtest can build a WorldModel per bar exactly as the live plane would.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import pandas as pd
from loguru import logger

from brain.fvg_detector import FVGDetector
from brain.inducement_detector import InducementDetector
from brain.instrument_profile import get_profile
from brain.liquidity_mapper import LiquidityMapper
from brain.order_block import OrderBlockDetector
from brain.structure_engine import StructureEngine, StructureAnalysis
from brain.volume_analyzer import VolumeAnalyzer
from brain.wyckoff_engine import WyckoffEngine
from brain.world_model import WorldModel, build_world_model
from brain.concept_modules import run_concepts
from config import get_pip_size
from entry.models import EntryConfig
from entry.zone_watcher import extract_entry_zones


# TF → brain modules to run when that timeframe closes.  Canonical home; the
# handler re-exports this so existing imports keep working.
TF_MODULE_MAP: dict[str, list[str]] = {
    "M5": ["fvg", "order_block", "volume", "inducement"],
    "M15": ["fvg"],
    "H1": ["fvg", "order_block", "liquidity", "volume", "wyckoff", "structure"],
    "H4": ["order_block", "liquidity", "structure"],
    "D1": ["structure"],
}

# Lowest → highest timeframe, used to pick the freshest current price.
_TF_BY_RECENCY = ("M5", "M15", "H1", "H4", "D1")


def compute_bias(struct_by_tf: dict[str, StructureAnalysis]) -> dict[str, Any]:
    """Synthesize a directional bias dict from per-TF StructureAnalysis.

    Direction is decided by H4 + H1 (D1 only weights confidence), mirroring
    ``StructureEngine.get_bias`` but operating on the already-computed analyses
    so no extra candle fetch is needed.  Returns the schema consumers expect:
    ``direction`` ("LONG"/"SHORT"/""), ``score`` (0-100), plus per-TF trends.
    """
    def _trend(tf: str) -> tuple[Optional[str], float]:
        sa = struct_by_tf.get(tf)
        if sa is None:
            return None, 0.0
        trend = sa.trend.value if hasattr(sa.trend, "value") else str(sa.trend)
        return trend, float(getattr(sa, "confidence", 0.0) or 0.0)

    h4_t, h4_c = _trend("H4")
    h1_t, h1_c = _trend("H1")
    d1_t, d1_c = _trend("D1")

    direction_trend: Optional[str] = None
    strength = "NONE"
    if h4_t and h4_t != "RANGING" and h4_t == h1_t:
        direction_trend, strength = h4_t, "STRONG"
    elif h4_t and h4_t != "RANGING" and (h1_t is None or h1_t == "RANGING"):
        direction_trend, strength = h4_t, "MODERATE"
    elif (h4_t is None or h4_t == "RANGING") and h1_t and h1_t != "RANGING":
        direction_trend, strength = h1_t, "MODERATE"
    elif h4_t and h1_t and h4_t != h1_t:
        strength = "CONFLICTED"

    if direction_trend == "BULLISH":
        direction = "LONG"
    elif direction_trend == "BEARISH":
        direction = "SHORT"
    else:
        direction = ""

    d1_aligned = bool(
        direction_trend and d1_t and d1_t != "RANGING" and d1_t == direction_trend
    )
    if d1_t and d1_t != "RANGING":
        if d1_aligned:
            confidence = round(d1_c * 0.4 + h4_c * 0.35 + h1_c * 0.25, 2)
        else:
            confidence = round(((h4_c + h1_c) / 2) * 0.85, 2)
    else:
        confidence = round((h4_c + h1_c) / 2, 2)

    score = int(round(confidence * 100)) if direction else 0

    return {
        "direction": direction,
        "score": score,
        "opposing_boost": 0,
        "strength": strength,
        "h4_trend": h4_t or "UNKNOWN",
        "h1_trend": h1_t or "UNKNOWN",
        "d1_trend": d1_t or "UNKNOWN",
        "d1_aligned": d1_aligned,
        "confidence": confidence,
        "tradeable": strength in ("STRONG", "MODERATE"),
    }


def run_tf_modules(
    symbol: str,
    tf: str,
    df: pd.DataFrame,
    *,
    structure: StructureEngine,
    liquidity: LiquidityMapper,
    volume: VolumeAnalyzer,
) -> dict[str, Any]:
    """Run the brain modules for one timeframe, plus the concept generators.

    Mirrors the live handler's per-timeframe analysis exactly; the shared
    ``structure``/``liquidity``/``volume`` engines are injected so callers can
    reuse instances.  Each module is guarded so one failure can't suppress the
    rest, and a concept failure can never break the ICT analysis.
    """
    modules = TF_MODULE_MAP.get(tf, [])
    if not modules:
        return {}

    try:
        pip_size = get_pip_size(symbol)
    except KeyError:
        pip_size = 0.0001
    profile = get_profile(symbol)
    results: dict[str, Any] = {}

    for mod in modules:
        try:
            if mod == "fvg":
                det = FVGDetector(
                    pip_size=pip_size,
                    proximity_pips=profile.fvg_proximity_pips,
                    min_size_pips=profile.fvg_min_size_pips,
                )
                results["fvg"] = det.detect(df, timeframe=tf)
            elif mod == "order_block":
                det = OrderBlockDetector(
                    pip_size=pip_size,
                    min_impulse_pips=profile.ob_min_impulse_pips,
                    buffer_pips=profile.ob_buffer_pips,
                )
                results["order_block"] = det.detect(df, timeframe=tf)
            elif mod == "liquidity":
                results["liquidity"] = liquidity.map(df, pip_size)
            elif mod == "volume":
                results["volume"] = volume.analyze(df)
            elif mod == "wyckoff":
                if profile.wyckoff_enabled:
                    wyck = WyckoffEngine(pip_size=pip_size)
                    results["wyckoff"] = wyck.analyze(df)
            elif mod == "inducement":
                det = InducementDetector(pip_size=pip_size)
                results["inducement"] = det.analyze(df)
            elif mod == "structure":
                results["structure"] = structure.analyze(df)
        except Exception as exc:
            logger.debug(
                "[decision-core] {} {} module '{}' failed: {}",
                symbol, tf, mod, exc,
            )

    try:
        signals, regime = run_concepts(df)
        results["concepts"] = signals
        results["regime"] = regime
    except Exception as exc:
        logger.debug(
            "[decision-core] {} {} concept modules failed: {}", symbol, tf, exc,
        )

    return results


def blend_concepts(
    bias: dict[str, Any],
    concepts_by_tf: dict[str, list],
    regime_by_tf: dict[str, str],
    concept_weight: Optional[Callable[..., float]] = None,
) -> dict[str, Any]:
    """Blend non-ICT concept signals into the structural bias.

    Each directional concept votes (LONG/SHORT) weighted by its strength and
    learned per-concept edge.  The net vote nudges the bias ``score`` within a
    bounded range and is recorded for observability — it never flips the
    structural ``direction`` or ``tradeable`` decision, so the ICT plane stays
    authoritative and the blend is default-neutral when concepts are neutral.
    """
    try:
        long_w = 0.0
        short_w = 0.0
        for tf, signals in concepts_by_tf.items():
            rg = regime_by_tf.get(tf)
            for sig in signals or []:
                if not getattr(sig, "is_directional", False):
                    continue
                w = 1.0
                if concept_weight is not None:
                    try:
                        w = float(concept_weight(sig.name, rg))
                    except Exception:  # noqa: BLE001
                        w = 1.0
                contrib = w * float(getattr(sig, "strength", 0.0) or 0.0)
                if sig.direction == "LONG":
                    long_w += contrib
                elif sig.direction == "SHORT":
                    short_w += contrib

        net = long_w - short_w
        concept_dir = "LONG" if net > 0 else ("SHORT" if net < 0 else "")
        magnitude = min(15.0, abs(net) * 10.0)

        out = dict(bias)
        direction = out.get("direction", "")
        score = float(out.get("score", 0) or 0)
        if direction and concept_dir:
            if concept_dir == direction:
                score = min(100.0, score + magnitude)
            else:
                score = max(0.0, score - magnitude)
        out["score"] = int(round(score))
        out["concept_direction"] = concept_dir
        out["concept_score"] = round(magnitude, 2)
        return out
    except Exception:  # noqa: BLE001 — blending must never break analysis
        return bias


def fvg_proximity(symbol: str) -> float:
    """FVG proximity (price units), matching the detector the ED path uses."""
    try:
        pip_size = get_pip_size(symbol)
    except KeyError:
        pip_size = 0.0001
    profile = get_profile(symbol)
    return FVGDetector(
        pip_size=pip_size,
        proximity_pips=profile.fvg_proximity_pips,
        min_size_pips=profile.fvg_min_size_pips,
    ).proximity


def build_consensus(
    symbol: str, wm: WorldModel, current_price: float,
) -> tuple[list, list]:
    """Derive per-module directional votes + ranked opportunities from a WM.

    Reuses the same vote extractors and opportunity ranker the legacy scanner
    used, sourced from the WorldModel's already-computed analysis (structure,
    volume, wyckoff, order blocks, FVGs).  Modules needing raw price series the
    ED plane does not retain (momentum, VWAP, currency strength, liquidity
    sweep) are omitted.  Each extractor is guarded.
    """
    from brain.directional_consensus import (
        Vote,
        vote_from_structure,
        vote_from_volume,
        vote_from_wyckoff,
        vote_from_order_blocks,
        vote_from_fvg,
        decide_opportunities,
    )

    votes: list = []

    try:
        b = wm.bias_dict()
        sdir = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(
            str(b.get("direction", "")).upper(), "RANGING",
        )
        r = vote_from_structure(
            {"direction": sdir,
             "confidence": float(b.get("confidence", 0.0) or 0.0)}
        )
        votes.append(Vote("structure", r[0], r[1], 3.0))
    except Exception:
        pass

    try:
        vbt = wm.volume_by_tf()
        va = vbt.get("M5") or vbt.get("H1")
        if va is not None:
            r = vote_from_volume(va)
            votes.append(Vote("volume", r[0], r[1], 1.0))
    except Exception:
        pass

    try:
        wy = wm.wyckoff_by_tf().get("H1")
        if wy is not None:
            r = vote_from_wyckoff(wy)
            votes.append(Vote("wyckoff", r[0], r[1], 1.5))
    except Exception:
        pass

    if current_price and current_price > 0:
        try:
            obs = wm.all_order_blocks()
            if obs:
                r = vote_from_order_blocks(list(obs), current_price)
                votes.append(Vote("order_block", r[0], r[1], 1.0))
        except Exception:
            pass
        try:
            fvgs = wm.all_fvgs()
            if fvgs:
                r = vote_from_fvg(list(fvgs), current_price, fvg_proximity(symbol))
                votes.append(Vote("fvg", r[0], r[1], 1.0))
        except Exception:
            pass

    candidates: list = []
    try:
        if votes:
            candidates = list(decide_opportunities(votes))
    except Exception:
        pass

    return votes, candidates


def analyze_window(
    symbol: str,
    candles_by_tf: dict[str, pd.DataFrame],
    *,
    version: int = 0,
    timestamp: Optional[datetime] = None,
    entry_config: Optional[EntryConfig] = None,
    edge_weight: Optional[Callable[..., float]] = None,
    concept_weight: Optional[Callable[..., float]] = None,
    structure: Optional[StructureEngine] = None,
    liquidity: Optional[LiquidityMapper] = None,
    volume: Optional[VolumeAnalyzer] = None,
) -> WorldModel:
    """One-shot: build a fully-populated WorldModel from a multi-TF candle set.

    Runs the same brain pipeline as the live handler across every timeframe
    present in ``candles_by_tf`` and synthesizes bias, concepts/regime, entry
    zones, and consensus votes/candidates — returning a complete WorldModel.
    Used by the backtest so it decides identically to the live plane.
    """
    structure = structure or StructureEngine()
    liquidity = liquidity or LiquidityMapper()
    volume = volume or VolumeAnalyzer()
    cfg = entry_config or EntryConfig()

    fvgs: dict[str, list] = {}
    obs: dict[str, list] = {}
    struct: dict[str, Any] = {}
    liq: dict[str, Any] = {}
    vol: dict[str, Any] = {}
    wyck: dict[str, Any] = {}
    ind: dict[str, Any] = {}
    concepts: dict[str, list] = {}
    regime: dict[str, str] = {}

    for tf in TF_MODULE_MAP:
        df = candles_by_tf.get(tf)
        if df is None or getattr(df, "empty", True):
            continue
        results = run_tf_modules(
            symbol, tf, df,
            structure=structure, liquidity=liquidity, volume=volume,
        )
        if "fvg" in results:
            fvgs[tf] = list(results["fvg"])
        if "order_block" in results:
            obs[tf] = list(results["order_block"])
        if "structure" in results:
            struct[tf] = results["structure"]
        if "liquidity" in results:
            liq[tf] = results["liquidity"]
        if "volume" in results:
            vol[tf] = results["volume"]
        if "wyckoff" in results:
            wyck[tf] = results["wyckoff"]
        if "inducement" in results:
            ind[tf] = results["inducement"]
        if "concepts" in results:
            concepts[tf] = list(results["concepts"])
        if "regime" in results:
            regime[tf] = results["regime"]

    # Freshest current price from the lowest available timeframe.
    current_price = 0.0
    for tf in _TF_BY_RECENCY:
        df = candles_by_tf.get(tf)
        if df is not None and not getattr(df, "empty", True):
            try:
                current_price = float(df["close"].iloc[-1])
                break
            except Exception:
                continue

    bias = compute_bias(struct)
    bias = blend_concepts(bias, concepts, regime, concept_weight)

    wm = build_world_model(
        symbol=symbol,
        version=version,
        timestamp=timestamp or datetime.now(timezone.utc),
        fvgs=fvgs,
        order_blocks=obs,
        structure=struct,
        liquidity=liq,
        volume=vol,
        wyckoff=wyck,
        inducement=ind,
        bias=bias,
        concepts=concepts,
        regime=regime,
    )

    zones = extract_entry_zones(wm, cfg, edge_weight)
    if zones:
        wm = replace(wm, entry_zones=tuple(zones))

    try:
        votes, candidates = build_consensus(symbol, wm, current_price)
        if votes or candidates:
            wm = replace(wm, votes=tuple(votes), candidates=tuple(candidates))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[decision-core] {} consensus build failed: {}", symbol, exc)

    return wm

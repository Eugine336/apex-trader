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
from config import get_pip_size, ConsensusConfig, _DEFAULT_CONSENSUS_WEIGHTS
from entry.models import EntryConfig
from entry.zone_watcher import extract_entry_zones


# ── Throttled failure visibility ─────────────────────────────────────────
# Consensus vote modules run per candle-close for every symbol/TF.  A module
# that fails must be *visible* (audit #18 — no silent ``except: pass``), but a
# per-cycle WARNING would flood the log.  Log the first failure of each
# (key, module) at WARNING with the exception, then throttle repeats to at
# most once per interval so a persistently-broken module stays surfaced
# without spamming.
import time as _time

_WARN_THROTTLE_S = 300.0
_last_warned: dict[str, float] = {}


def _warn_module_failure(scope: str, module: str, symbol: str, exc: Exception) -> None:
    key = f"{scope}:{module}:{symbol}"
    now = _time.monotonic()
    last = _last_warned.get(key, 0.0)
    if now - last >= _WARN_THROTTLE_S:
        _last_warned[key] = now
        logger.warning(
            "[consensus] {} module '{}' failed for {}: {} — {}",
            scope, module, symbol, type(exc).__name__, exc,
        )


# TF → brain modules to run when that timeframe closes.  Canonical home; the
# handler re-exports this so existing imports keep working.
TF_MODULE_MAP: dict[str, list[str]] = {
    "M5": ["fvg", "order_block", "volume", "inducement", "structure"],
    "M15": ["fvg", "structure"],
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
    except KeyError as exc:
        logger.error(
            "[decision-core] no registered pip_size for {} — refusing to run "
            "tf modules with a wrong default (would corrupt geometry): {}",
            symbol, exc,
        )
        raise
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
                results["structure"] = structure.analyze(df, pip_size=pip_size)
        except Exception as exc:
            _warn_module_failure(f"tf-{tf}", mod, symbol, exc)

    try:
        signals, regime = run_concepts(df)
        results["concepts"] = signals
        results["regime"] = regime
    except Exception as exc:
        _warn_module_failure(f"tf-{tf}", "concepts", symbol, exc)

    return results


def blend_concepts(
    bias: dict[str, Any],
    concepts_by_tf: dict[str, list],
    regime_by_tf: dict[str, str],
    concept_weight: Optional[Callable[..., float]] = None,
    *,
    concept_flip_threshold: float = 0.0,
) -> dict[str, Any]:
    """Blend non-ICT concept signals into the structural bias.

    Each directional concept votes (LONG/SHORT) weighted by its strength and
    learned per-concept edge.  The net vote nudges the bias ``score`` within a
    bounded range and is recorded for observability.

    By default (``concept_flip_threshold <= 0``) the blend never flips the
    structural ``direction`` — it only nudges the score, keeping the ICT plane
    authoritative.  When ``concept_flip_threshold > 0`` and the absolute net
    concept vote clears it while opposing structure, the bias direction is
    allowed to flip to the concept direction: overwhelming, convergent market
    evidence is no longer discarded in favour of the structural label.
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
        out["concept_net"] = round(net, 4)
        out["concept_flip"] = False

        # Direction flip: when the concept panel overwhelmingly disagrees with
        # structure (|net| past the configured threshold), let the market
        # evidence flip the blended direction instead of merely shaving score.
        if (
            concept_flip_threshold
            and concept_flip_threshold > 0
            and direction
            and concept_dir
            and concept_dir != direction
            and abs(net) >= concept_flip_threshold
        ):
            out["direction"] = concept_dir
            out["score"] = int(round(min(100.0, magnitude)))
            out["concept_flip"] = True
            out["concept_flip_from"] = direction
            logger.info(
                "[concepts] bias flipped {}→{} on concept net={:+.2f} "
                "(threshold {:.2f})",
                direction, concept_dir, net, concept_flip_threshold,
            )
        return out
    except Exception:  # noqa: BLE001 — blending must never break analysis
        return bias


def fvg_proximity(symbol: str) -> float:
    """FVG proximity (price units), matching the detector the ED path uses."""
    try:
        pip_size = get_pip_size(symbol)
    except KeyError as exc:
        logger.error(
            "[decision-core] no registered pip_size for {} — cannot compute "
            "fvg proximity with a wrong default: {}", symbol, exc,
        )
        raise
    profile = get_profile(symbol)
    return FVGDetector(
        pip_size=pip_size,
        proximity_pips=profile.fvg_proximity_pips,
        min_size_pips=profile.fvg_min_size_pips,
    ).proximity


def build_consensus(
    symbol: str,
    wm: WorldModel,
    current_price: float,
    *,
    m5_df: Optional[pd.DataFrame] = None,
    h1_df: Optional[pd.DataFrame] = None,
    liquidity_mapper: Any = None,
    session_open_minutes: Optional[int] = None,
    currency_strength_analysis: Any = None,
    currency_pairs: Optional[dict[str, tuple[str, str]]] = None,
    correlation_signal: Any = None,
    vote_calibrator: Any = None,
    module_governor: Any = None,
    win_rate_provider: Any = None,
    weights: Optional[dict[str, float]] = None,
) -> tuple[list, list]:
    """Derive per-module directional votes + ranked opportunities from a WM.

    Reuses the same vote extractors and opportunity ranker the legacy scanner
    used, sourced from the WorldModel's already-computed analysis (structure,
    volume, wyckoff, order blocks, FVGs, inducement) and optional raw-series
    inputs for momentum/VWAP/volatility/liquidity/currency-strength when provided
    by the caller. Each extractor is guarded.

    The full analyst roster votes here so the consensus panel is complete:
    structure, volume, wyckoff, order_block, fvg, liquidity, momentum, vwap,
    currency_strength, **inducement**, **volatility**, and **correlation**.
    Inducement reads its reversal direction straight off the WorldModel.
    Volatility confirms the structural bias only when ATR is expanding (it
    abstains in compression — a coiling market has no directional edge).
    Correlation is intermarket *confirmation* (distinct from Portfolio's
    exposure management) and abstains until a cross-pair ``correlation_signal``
    is supplied — the per-symbol WorldModel carries none today, so it is
    behaviour-neutral by default.

    The optional ``vote_calibrator`` and ``module_governor`` close the adaptive
    feedback loop on the vote panel itself: a module the
    :class:`~adaptive.module_governor.ModuleGovernor` has SHADOWED/DISABLED is
    excluded from the panel entirely, and every surviving vote's static
    ``ConsensusConfig`` weight is scaled by the
    :class:`~adaptive.vote_calibrator.VoteCalibrator` multiplier (a module that
    predicts well votes louder, one that is mostly noise votes softer). Both are
    no-ops when their feature flag is off — the calibrator returns the base
    weight unchanged and the governor reports nothing suppressed — so wiring
    them in is behaviour-neutral until the operator enables them.
    """
    from brain.directional_consensus import (
        Vote,
        vote_from_structure,
        vote_from_currency_strength,
        vote_from_volume,
        vote_from_wyckoff,
        vote_from_order_blocks,
        vote_from_fvg,
        vote_from_liquidity,
        vote_from_momentum,
        vote_from_vwap,
        vote_from_inducement,
        vote_from_volatility,
        vote_from_correlation,
        decide_opportunities,
    )

    # Per-module base weights come from ``ConsensusConfig.weights`` so the panel
    # is operator-tunable and unbiased by default (every module starts at 1.0 —
    # equal footing). Falls back to the config defaults when no override is
    # passed, so structure no longer carries a hardcoded structural advantage.
    base_weights = dict(_DEFAULT_CONSENSUS_WEIGHTS)
    if weights:
        for k, v in weights.items():
            try:
                base_weights[k] = float(v)
            except (TypeError, ValueError):
                continue

    def _wt(module: str) -> float:
        return float(base_weights.get(module, 1.0))

    votes: list = []

    def _add_vote(
        module: str,
        direction: str,
        confidence: float,
        base_weight: float,
        *,
        timeframe: str = "",
        evidence: Optional[dict] = None,
    ) -> None:
        """Append a Vote after applying governor suppression + calibrated weight.

        Both adaptive hooks are guarded so a malfunctioning learner can never
        block the panel: on any error the module is kept with its base weight.
        """
        # Module Governor: exclude SHADOWED / DISABLED modules from the panel.
        # Pass ``symbol`` so the governor can apply its per-symbol isolation
        # overlay (1C) — a module is suppressed only where it is actually
        # harmful, not globally because of another instrument's track record.
        if module_governor is not None:
            try:
                try:
                    suppressed = module_governor.is_suppressed(module, symbol)
                except TypeError:
                    # Older governor without the per-symbol parameter.
                    suppressed = module_governor.is_suppressed(module)
                if suppressed:
                    return
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[consensus] {} governor check failed for {}: {}",
                    symbol, module, exc,
                )
        # Vote Calibrator: scale the static consensus weight by the learned,
        # bounded, mean-1.0 multiplier (unchanged when calibration is off). The
        # ``symbol`` lets the calibrator prefer its per-symbol overlay (1C).
        weight = base_weight
        if vote_calibrator is not None:
            try:
                try:
                    weight = vote_calibrator.calibrated_weight(module, base_weight, symbol)
                except TypeError:
                    # Older calibrator without the per-symbol parameter.
                    weight = vote_calibrator.calibrated_weight(module, base_weight)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[consensus] {} calibrate failed for {}: {}",
                    symbol, module, exc,
                )
                weight = base_weight
        votes.append(
            Vote(module, direction, confidence, weight,
                 timeframe=timeframe, evidence=evidence or {})
        )

    try:
        b = wm.bias_dict()
        sdir = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(
            str(b.get("direction", "")).upper(), "RANGING",
        )
        r = vote_from_structure(
            {"direction": sdir,
             "confidence": float(b.get("confidence", 0.0) or 0.0)}
        )
        _add_vote("structure", r[0], r[1], _wt("structure"))
    except Exception as exc:
        _warn_module_failure("consensus", "structure", symbol, exc)

    try:
        vbt = wm.volume_by_tf()
        va = vbt.get("M5") or vbt.get("H1")
        if va is not None:
            r = vote_from_volume(va)
            _add_vote("volume", r[0], r[1], _wt("volume"))
    except Exception as exc:
        _warn_module_failure("consensus", "volume", symbol, exc)

    try:
        wy = wm.wyckoff_by_tf().get("H1")
        if wy is not None:
            r = vote_from_wyckoff(wy)
            _add_vote("wyckoff", r[0], r[1], _wt("wyckoff"))
    except Exception as exc:
        _warn_module_failure("consensus", "wyckoff", symbol, exc)

    try:
        ind_by_tf = wm.inducement_by_tf()
        ia = (
            ind_by_tf.get("M5")
            or ind_by_tf.get("H1")
            or next(iter(ind_by_tf.values()), None)
        )
        if ia is not None:
            r = vote_from_inducement(ia)
            _add_vote(
                "inducement", r[0], r[1], _wt("inducement"),
                evidence=getattr(r, "evidence", {}) or {},
            )
    except Exception as exc:
        _warn_module_failure("consensus", "inducement", symbol, exc)

    if current_price and current_price > 0:
        try:
            obs = wm.all_order_blocks()
            if obs:
                r = vote_from_order_blocks(list(obs), current_price)
                _add_vote("order_block", r[0], r[1], _wt("order_block"))
        except Exception as exc:
            _warn_module_failure("consensus", "order_block", symbol, exc)
        try:
            fvgs = wm.all_fvgs()
            if fvgs:
                r = vote_from_fvg(list(fvgs), current_price, fvg_proximity(symbol))
                _add_vote("fvg", r[0], r[1], _wt("fvg"))
        except Exception as exc:
            _warn_module_failure("consensus", "fvg", symbol, exc)

    if m5_df is not None and len(m5_df) > 0:
        try:
            if liquidity_mapper is not None:
                try:
                    pip_size = get_pip_size(symbol)
                except Exception as exc:
                    logger.error(
                        "[decision-core] no registered pip_size for {} — "
                        "skipping liquidity vote rather than using a wrong "
                        "default: {}", symbol, exc,
                    )
                    raise
                r = vote_from_liquidity(liquidity_mapper, m5_df, pip_size)
                _add_vote(
                    "liquidity", r[0], r[1], _wt("liquidity"), timeframe="M5",
                    evidence=getattr(r, "evidence", {}) or {},
                )
        except Exception as exc:
            _warn_module_failure("consensus", "liquidity", symbol, exc)
        try:
            r = vote_from_momentum(m5_df, h1_df)
            _add_vote(
                "momentum", r[0], r[1], _wt("momentum"), timeframe="M5",
                evidence=getattr(r, "evidence", {}) or {},
            )
        except Exception as exc:
            _warn_module_failure("consensus", "momentum", symbol, exc)
        try:
            mins = int(session_open_minutes) if session_open_minutes is not None else 0
            r = vote_from_vwap(
                m5_df, mins, float(current_price or 0.0),
            )
            _add_vote(
                "vwap", r[0], r[1], _wt("vwap"), timeframe="M5",
                evidence=getattr(r, "evidence", {}) or {},
            )
        except Exception as exc:
            _warn_module_failure("consensus", "vwap", symbol, exc)
        try:
            bdir = str(wm.bias_dict().get("direction", "") or "").upper()
            r = vote_from_volatility(m5_df, bdir)
            _add_vote(
                "volatility", r[0], r[1], _wt("volatility"), timeframe="M5",
                evidence=getattr(r, "evidence", {}) or {},
            )
        except Exception as exc:
            _warn_module_failure("consensus", "volatility", symbol, exc)

    if currency_strength_analysis is not None:
        try:
            r = vote_from_currency_strength(
                symbol,
                currency_strength_analysis,
                currency_pairs or {},
            )
            _add_vote(
                "currency_strength", r[0], r[1], _wt("currency_strength"),
                evidence=getattr(r, "evidence", {}) or {},
            )
        except Exception as exc:
            _warn_module_failure("consensus", "currency_strength", symbol, exc)

    # Correlation is a placeholder voter: the per-symbol WorldModel carries no
    # cross-pair ``correlation_signal`` today, so this block never executes live
    # and the module always abstains until an intermarket feed is wired in.
    if correlation_signal is not None:
        try:
            r = vote_from_correlation(correlation_signal)
            _add_vote(
                "correlation", r[0], r[1], _wt("correlation"),
                evidence=getattr(r, "evidence", {}) or {},
            )
        except Exception as exc:
            _warn_module_failure("consensus", "correlation", symbol, exc)

    candidates: list = []
    try:
        if votes:
            # Resolve the per-pair calibrated win-rate callable for the ranker
            # so opportunity EV uses learned PairLearner/EVEstimator rates
            # instead of the hardcoded prior. Behaviour-neutral when no provider
            # is wired or the pair has no history (falls back to the prior).
            wr_callable = None
            if win_rate_provider is not None:
                try:
                    regime = ""
                    try:
                        rb = wm.regime_by_tf()
                        regime = (
                            rb.get("H1") or rb.get("M5")
                            or next(iter(rb.values()), "")
                        )
                    except Exception:
                        regime = ""
                    wr_callable, _ = win_rate_provider.for_pair(
                        symbol, regime=regime or "",
                    )
                except Exception as exc:
                    logger.debug(
                        "[consensus] {} win-rate provider resolve failed: {}",
                        symbol, exc,
                    )
                    wr_callable = None
            candidates = list(
                decide_opportunities(votes, win_rate_provider=wr_callable)
            )
    except Exception as exc:
        _warn_module_failure("consensus", "decide_opportunities", symbol, exc)

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
    consensus_config: Optional[ConsensusConfig] = None,
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
    cc = consensus_config or ConsensusConfig()

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
    bias = blend_concepts(
        bias, concepts, regime, concept_weight,
        concept_flip_threshold=cc.concept_flip_threshold,
    )

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
        votes, candidates = build_consensus(
            symbol,
            wm,
            current_price,
            m5_df=candles_by_tf.get("M5"),
            h1_df=candles_by_tf.get("H1"),
            liquidity_mapper=liquidity,
            weights=cc.weights,
        )
        if votes or candidates:
            wm = replace(wm, votes=tuple(votes), candidates=tuple(candidates))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[decision-core] {} consensus build failed: {}", symbol, exc)

    # Synthesize the shared setup-quality layer (real OQ/EQ + regime analysis)
    # so the WorldModel carries identical quality signals in live and backtest.
    try:
        from brain.quality_layer import compute_quality_layer

        ql = compute_quality_layer(
            symbol,
            wm,
            m5_df=candles_by_tf.get("M5"),
            h1_df=candles_by_tf.get("H1"),
            current_price=current_price,
        )
        wm = replace(
            wm,
            opportunity_quality=ql["opportunity_quality"],
            entry_quality_long=ql["entry_quality_long"],
            entry_quality_short=ql["entry_quality_short"],
            regime_analysis=ql["regime_analysis"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[decision-core] {} quality layer failed: {}", symbol, exc)

    return wm

"""Market-aware strategy-knowledge map.

The registry describes analytical lenses that Apex knows how to consider. It is
not an entry-rule engine, strategy selector that places orders, or a claim that
every listed method is implemented end-to-end. Relevance is surfaced from current
evidence so the Brain can decide what deserves attention; no lens can authorize a
trade, produce a direction, or bypass qualification and risk controls.

A lens may be relevant, worth watching, or not assessable because its required
market data is absent. Missing data is explicit rather than fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class StrategyLens:
    key: str
    name: str
    purpose: str
    evidence_domains: tuple[str, ...]
    anchors: tuple[str, ...]
    data_requirements: tuple[str, ...]
    limitation: str


@dataclass(frozen=True)
class StrategyLensAssessment:
    key: str
    name: str
    status: str
    relevance: float
    reasons: tuple[str, ...]
    missing_context: tuple[str, ...]
    limitation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "status": self.status,
            "relevance": round(self.relevance, 3),
            "reasons": list(self.reasons),
            "missing_context": list(self.missing_context),
            "limitation": self.limitation,
        }


STRATEGY_LENSES: tuple[StrategyLens, ...] = (
    StrategyLens("ict_smc", "ICT / Smart Money Concepts",
        "Interpret structure, displacement, imbalances, inducement and liquidity.",
        ("structure", "liquidity", "order_flow"),
        ("fvg", "order_block", "inducement", "liquidity", "displacement", "bos", "choch"),
        ("completed candles", "confirmed structural levels"),
        "Named concepts are hypotheses about price behaviour, not proof of institutional orders."),
    StrategyLens("trend_following", "Trend following",
        "Assess persistent direction, pullbacks and continuation versus exhaustion.",
        ("structure", "momentum", "multi_timeframe"),
        ("trend", "continuation", "multi_tf_aligned", "momentum_acceleration", "ema"),
        ("multiple observations across time"),
        "Moving-average and structure signals lag and can fail in ranges."),
    StrategyLens("momentum", "Momentum / impulse",
        "Assess acceleration, deceleration, displacement and follow-through.",
        ("momentum", "volume", "structure"),
        ("momentum_acceleration", "displacement", "volume_confirmed_breakout", "delta"),
        ("recent price changes", "volume or tick-flow proxy"),
        "Momentum can be exhausted or distorted by gaps, news and thin markets."),
    StrategyLens("breakout", "Range breakout / expansion",
        "Assess acceptance beyond a meaningful range and the quality of expansion.",
        ("structure", "volatility", "volume"),
        ("breakout", "volatility_expansion", "volume_confirmed_breakout", "bos", "displacement"),
        ("defined range", "completed breakout candle"),
        "A boundary crossing alone does not distinguish acceptance from a false break."),
    StrategyLens("failed_breakout", "Failed breakout / trap",
        "Assess failed acceptance, rejection, reclaim and trapped-side risk.",
        ("structure", "liquidity", "momentum"),
        ("failed_breakout", "liquidity_sweep", "sweep", "rejection", "absorption"),
        ("prior range boundaries", "post-break reaction"),
        "A wick is not a reversal thesis without context and invalidation."),
    StrategyLens("mean_reversion", "Mean reversion",
        "Assess stretched price, contained trend strength and evidence of reversion.",
        ("momentum", "volatility", "structure"),
        ("statistical_displacement", "rolling_zscore", "mean_reversion", "range"),
        ("rolling price distribution", "trend-strength context"),
        "An extreme can become more extreme; statistical stationarity must be tested."),
    StrategyLens("range_auction", "Range trading / market auction",
        "Assess rotation, value acceptance/rejection and range boundaries.",
        ("structure", "volume", "volatility"),
        ("range", "consolidation", "value_area", "poc", "vwap", "rejection"),
        ("reliable range boundaries", "volume/auction data where available"),
        "Value-area or point-of-control analysis requires suitable volume data."),
    StrategyLens("candle_price_action", "Candlestick / M1 price action",
        "Use engulfing candles, pin bars, rejection wicks and inside-bar breaks as contextual evidence.",
        ("structure", "momentum", "liquidity"),
        ("m1_price_action", "engulfing", "pin_bar", "rejection_wick", "inside_bar_breakout"),
        ("recent M1 OHLC", "relevant location or structural context"),
        "A candle pattern alone is not an entry; forming candles may repaint."),
    StrategyLens("order_flow_auction", "Order flow / volume auction",
        "Assess participation, absorption, imbalances, volume spikes and auction response.",
        ("order_flow", "volume", "structure"),
        ("order_flow", "imbalance", "volume_spike", "absorption", "delta", "volume_profile"),
        ("broker volume/ticks; depth or aggressor data for advanced claims"),
        "Tick volume and top-of-book are not a complete centralized order book."),
    StrategyLens("volatility_regime", "Volatility regime / compression",
        "Distinguish compression, stable conditions, expansion and unstable movement.",
        ("volatility", "structure"),
        ("volatility_compression", "volatility_expansion", "atr", "squeeze", "expanding", "contracting"),
        ("recent and baseline range measurements"),
        "High volatility is not itself directional and can worsen fills."),
    StrategyLens("wyckoff", "Wyckoff market-cycle analysis",
        "Evaluate accumulation/distribution hypotheses, springs, upthrusts and phase transitions.",
        ("structure", "volume", "liquidity"),
        ("wyckoff", "accumulation", "distribution", "spring", "upthrust"),
        ("range structure", "volume/participation evidence"),
        "Phase labels are interpretations and require contextual validation."),
    StrategyLens("crt", "Candle Range Theory",
        "Observe anchor-candle boundaries, sweep/reclaim and lower-timeframe structure.",
        ("structure", "liquidity", "multi_timeframe"),
        ("crt_range_observation", "candle range theory", "anchor_high", "anchor_low"),
        ("confirmed H1/H4/D1 anchor and lower-timeframe candles"),
        "Anchor choice and timeframe must be explicit; an anchor is not an automatic target."),
    StrategyLens("session_intraday", "Session / VWAP / intraday context",
        "Assess session liquidity, VWAP location, opening ranges and timing context.",
        ("session", "structure", "volume"),
        ("session", "london", "new york", "killzone", "vwap", "opening range"),
        ("timestamps", "session calendar", "VWAP/volume data where relevant"),
        "Session-specific patterns vary by instrument and DST/calendar details."),
    StrategyLens("event_macro", "Macro / event-driven",
        "Evaluate verified event context, pre-event ranges and post-release repricing.",
        ("macro", "volatility", "structure"),
        ("news", "calendar", "cpi", "fomc", "nfp", "event_window_breakout"),
        ("verified event name/time/impact", "market-reaction data"),
        "Event headlines require timestamps and trustworthy provenance; direction is not assumed."),
    StrategyLens("cross_asset", "Intermarket / relative strength",
        "Consider currencies, related instruments, correlation and cross-market confirmation.",
        ("correlation", "multi_timeframe", "macro"),
        ("correlation", "currency_strength", "dxy", "gold", "relative strength"),
        ("synchronized related-instrument data"),
        "Correlation is regime-dependent and does not prove causation."),
    StrategyLens("statistical_quant", "Statistical / quantitative methods",
        "Assess distributions, conditional expectancy, volatility scaling and historical analogues.",
        ("momentum", "historical_analogue", "volatility"),
        ("zscore", "rolling", "historical", "analogue", "expectancy", "distribution"),
        ("sufficient samples", "out-of-sample validation"),
        "Small samples, leakage and regime changes can invalidate attractive statistics."),
    StrategyLens("microstructure", "Tick / spread / execution microstructure",
        "Assess velocity, drift, spread behaviour, absorption proxies and executable costs.",
        ("execution_quality", "order_flow", "momentum"),
        ("tick", "velocity", "spread", "slippage", "latency", "execution"),
        ("fresh ticks/quotes", "broker cost and contract specifications"),
        "Latency and available data limit what can be inferred between ticks."),
)


def _domain(evidence: Any) -> str:
    value = getattr(evidence, "domain", "")
    return str(getattr(value, "value", value) or "").lower()


def _surface(evidence: Any) -> str:
    pieces = [
        str(getattr(evidence, "source_module", "") or ""),
        str(getattr(evidence, "observation", "") or ""),
    ]
    measurements = getattr(evidence, "measurements", {})
    if isinstance(measurements, dict):
        pieces.extend(str(k) for k in measurements)
        pieces.extend(str(v) for v in measurements.values() if isinstance(v, (str, bool)))
        patterns = measurements.get("pattern_tags")
        if isinstance(patterns, (list, tuple, set)):
            pieces.extend(str(item) for item in patterns)
    return " ".join(pieces).lower()


def assess_strategy_lenses(
    evidence: Iterable[Any],
    pattern_names: Iterable[str] = (),
    regime: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Assess analytical lens relevance from current evidence, never authorize trades.

    High relevance means the evidence makes a lens worth considering; it does not
    mean its setup exists, predicts direction, or is executable.
    """
    items = [item for item in (evidence or []) if item is not None]
    names = {str(name or "").lower() for name in pattern_names}
    reg = {str(k).lower(): str(v).lower() for k, v in (regime or {}).items()}
    domains = {_domain(item) for item in items}
    surfaces = " ".join([*(names), *(reg.values()), *(_surface(item) for item in items)])
    assessments: list[StrategyLensAssessment] = []

    for lens in STRATEGY_LENSES:
        reasons: list[str] = []
        matched = [anchor for anchor in lens.anchors if anchor.lower() in surfaces]
        if matched:
            reasons.append("market evidence mentions: " + ", ".join(matched[:5]))
        domain_hits = [domain for domain in lens.evidence_domains if domain in domains]
        if domain_hits:
            reasons.append("available evidence domains: " + ", ".join(domain_hits))
        regime_hits = [
            f"{key}={value}" for key, value in reg.items()
            if value and value not in {"unknown", "none", "balanced", "flat"}
            and any(token in value for token in lens.anchors)
        ]
        if regime_hits:
            reasons.append("regime context: " + ", ".join(regime_hits[:3]))

        raw_score = len(matched) + min(2, len(domain_hits)) + min(1, len(regime_hits))
        missing = [
            requirement for requirement in lens.data_requirements
            if not all(
                token in surfaces
                for token in requirement.lower().split()
                if len(token) > 3
            )
        ]
        if raw_score >= 2:
            status = "relevant"
        elif raw_score == 1:
            status = "watch"
        else:
            status = "not_evaluable"
        if not reasons:
            reasons.append("no specific current evidence anchor; do not assume this lens fits")
        assessments.append(StrategyLensAssessment(
            key=lens.key,
            name=lens.name,
            status=status,
            relevance=min(1.0, raw_score / 5.0),
            reasons=tuple(reasons),
            missing_context=tuple(missing),
            limitation=lens.limitation,
        ))

    order = {"relevant": 0, "watch": 1, "not_evaluable": 2}
    assessments.sort(key=lambda item: (order[item.status], -item.relevance, item.name))
    return [assessment.to_dict() for assessment in assessments]


__all__ = ["StrategyLens", "StrategyLensAssessment", "STRATEGY_LENSES", "assess_strategy_lenses"]

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
    data_requirements: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "status": self.status,
            "relevance": round(self.relevance, 3),
            "reasons": list(self.reasons),
            "missing_context": list(self.missing_context),
            "data_requirements": list(self.data_requirements),
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

    StrategyLens("support_resistance", "Support / resistance and market structure",
        "Assess repeated reactions, swing levels, break-and-retest and structural invalidation.",
        ("structure", "liquidity"), ("support", "resistance", "swing_high", "swing_low", "break_retest", "structure"),
        ("multiple confirmed reactions", "level age and distance"),
        "Levels are zones with uncertainty; repeated touches do not guarantee another reaction."),
    StrategyLens("moving_average", "Moving-average context",
        "Use moving averages as lagging trend and location context rather than standalone triggers.",
        ("momentum", "multi_timeframe"), ("ema", "sma", "moving average", "ema_spread_atr", "ma_cross"),
        ("moving-average values", "lookback context"),
        "Moving averages lag and whipsaw in ranges; a cross alone is not an opportunity."),
    StrategyLens("oscillator_divergence", "Oscillator / momentum divergence",
        "Compare price structure with independently measured momentum and oscillator divergence.",
        ("momentum", "structure"), ("rsi", "macd", "stochastic", "divergence", "momentum_divergence"),
        ("price swings", "oscillator series"),
        "Divergence can persist and must not be treated as a timing signal by itself."),
    StrategyLens("trend_strength", "Trend strength / ADX-style regime",
        "Distinguish directional persistence from directionless movement and trend transitions.",
        ("momentum", "volatility", "structure"), ("adx", "trend_strength", "trend_state", "directional_index"),
        ("trend-strength history", "multiple bars"),
        "Trend-strength measures generally describe persistence, not direction or future return."),
    StrategyLens("fibonacci_geometry", "Retracement / measured-move geometry",
        "Treat retracement and measured-move zones as conditional location references.",
        ("structure",), ("fibonacci", "retracement", "extension", "measured_move", "premium", "discount"),
        ("confirmed swing anchors", "instrument-specific price scale"),
        "Anchor selection is subjective; geometric confluence is not independent evidence."),
    StrategyLens("gap_opening_range", "Gaps / opening-range behaviour",
        "Assess gaps, opening ranges, overnight repricing and subsequent acceptance or rejection.",
        ("structure", "volatility", "session"), ("gap", "opening_range", "overnight", "session_open"),
        ("session boundary", "reliable time series"),
        "Many spot FX/CFD feeds have broker-specific gaps and no centralized session open."),
    StrategyLens("volatility_breakout", "Volatility contraction and expansion",
        "Evaluate range compression, realized volatility changes and expansion without assuming direction.",
        ("volatility", "structure"), ("atr", "squeeze", "compression", "expansion", "realized_volatility", "bollinger"),
        ("recent ranges", "historical volatility baseline"),
        "Expansion can be two-sided and increases slippage and stop distance."),
    StrategyLens("seasonality_cycles", "Seasonality / cycle hypotheses",
        "Test recurring time-of-day, day-of-week and calendar effects against an appropriate baseline.",
        ("historical_analogue", "session", "volatility"), ("seasonality", "cycle", "day_of_week", "time_of_day", "calendar_effect"),
        ("timestamped multi-period history", "holdout evaluation"),
        "Seasonality is vulnerable to multiple testing, sample selection and regime drift."),
    StrategyLens("relative_value", "Relative value / pair relationships",
        "Assess relative strength, spreads and residual behaviour between related instruments.",
        ("correlation", "historical_analogue"), ("relative_value", "cointegration", "pairs", "spread_zscore", "relative_strength"),
        ("synchronized histories", "stable relationship test"),
        "Correlation is not cointegration; relationship breakdown can dominate historical fit."),
    StrategyLens("carry_funding", "Carry / financing context",
        "Include swaps, financing, dividends and holding-period costs in multi-session hypotheses.",
        ("execution_quality", "macro"), ("swap", "carry", "funding", "financing", "rollover"),
        ("verified broker contract costs", "expected holding period"),
        "Financing schedules vary by broker, instrument and rollover date."),
    StrategyLens("news_event_reaction", "Verified news-event reaction",
        "Separate event facts, event timing, surprise and the observed market reaction.",
        ("macro", "volatility", "execution_quality"), ("news", "calendar", "cpi", "fomc", "nfp", "event_window"),
        ("timestamped trusted event source", "pre/post event market observations"),
        "Headlines do not provide a reliable direction; stale or unverified news is unusable."),
    StrategyLens("correlation_exposure", "Correlation and portfolio concentration",
        "Evaluate correlated exposure, shared risk factors and concentration across open campaigns.",
        ("correlation", "portfolio"), ("correlation", "concentration", "portfolio", "currency_exposure", "shared_factor"),
        ("whole-book positions", "synchronized instrument returns"),
        "Correlations are regime-dependent and portfolio data must reflect broker truth."),
    StrategyLens("execution_costs", "Execution costs and fill quality",
        "Estimate spread, commission, swap, slippage, latency and fill probability before action.",
        ("execution_quality", "portfolio"), ("spread", "commission", "swap", "slippage", "latency", "fill_probability", "execution"),
        ("fresh quotes", "verified broker cost model"),
        "Unverified cost defaults may inform diagnostics but cannot prove executable net expectancy."),
    StrategyLens("risk_of_ruin", "Risk of ruin / capital survival",
        "Evaluate exposure, drawdown, tail loss, gap risk and the consequences of correlated failures.",
        ("portfolio", "execution_quality"), ("drawdown", "risk_of_ruin", "tail_risk", "exposure", "margin", "leverage"),
        ("equity and open risk", "contract and margin specifications"),
        "Risk estimates are model-dependent; stop orders do not cap gap loss."),
    StrategyLens("auction_market_profile", "Market profile / auction theory",
        "Assess value acceptance, rejection, balance, imbalance and rotation where suitable data exists.",
        ("structure", "volume"), ("market_profile", "value_area", "point_of_control", "poc", "acceptance", "rejection"),
        ("reliable volume-at-price or explicitly marked proxy", "session boundaries"),
        "OHLC bars alone cannot reconstruct true volume-at-price or centralized auction data."),
    StrategyLens("wyckoff_effort_result", "Wyckoff effort versus result",
        "Compare participation changes with price progress to form accumulation/distribution hypotheses.",
        ("structure", "volume", "liquidity"), ("effort_result", "absorption", "spring", "upthrust", "accumulation", "distribution"),
        ("comparable volume history", "range and follow-through"),
        "Volume proxies differ by venue; phase labels are interpretations, not facts."),
    StrategyLens("pattern_discovery", "Novel pattern / anomaly discovery",
        "Identify repeatable conditional relationships without requiring a named textbook setup.",
        ("historical_analogue", "momentum", "volatility"), ("anomaly", "historical_analogue", "pattern_discovery", "outlier", "conditional_expectancy"),
        ("point-in-time feature history", "multiple-testing control", "untouched holdout"),
        "Discovery is not validation; data leakage and selection bias can create false edges."),
    StrategyLens("execution_order_types", "Order-type and execution policy",
        "Compare market, limit and stop order trade-offs against current spread and urgency.",
        ("execution_quality", "portfolio"), ("market_order", "limit_order", "stop_order", "pending_order", "fill_probability"),
        ("current quote/depth if available", "broker order constraints"),
        "Order type changes fill probability and adverse selection; it does not create edge."),
    StrategyLens("learning_calibration", "Outcome attribution and calibration",
        "Measure forecast calibration and method-specific net outcomes with sample-aware validation.",
        ("historical_analogue", "reasoning"), ("calibration", "brier", "walk_forward", "out_of_sample", "attribution", "drift"),
        ("timestamped decisions and outcomes", "cost-aware samples", "holdout split"),
        "Small or selectively recorded samples cannot justify live self-modification."),
    StrategyLens("derivatives_context", "Derivatives and contract-specific context",
        "Account for contract size, tick value, expiry, roll, margin and product-specific mechanics.",
        ("execution_quality", "macro"), ("contract_size", "tick_value", "expiry", "roll", "margin", "futures_basis"),
        ("verified instrument specification", "broker calendar"),
        "Instrument mechanics differ across brokers and symbols; missing specifications block sizing."),
    StrategyLens("market_regime_transition", "Regime transition / change-point",
        "Detect whether prior trend, range or volatility assumptions are losing explanatory value.",
        ("structure", "volatility", "historical_analogue"), ("regime_change", "change_point", "transition", "drift", "structural_break"),
        ("rolling and baseline observations", "minimum sample history"),
        "Change-point alarms lag and can overreact to isolated shocks."),
)


def _domain(evidence: Any) -> str:
    value = getattr(evidence, "domain", "")
    return str(getattr(value, "value", value) or "").lower()


def _surface(evidence: Any) -> str:
    """Build a normalized searchable surface from one evidence record."""
    # Source names are provenance, not evidence that a concept is present.
    # Including module paths here would make every market_lenses.*.structure
    # record look like support/resistance evidence solely because of its filename.
    pieces = [str(getattr(evidence, "observation", "") or "")]
    measurements = getattr(evidence, "measurements", {})
    if isinstance(measurements, dict):
        pieces.extend(str(k) for k in measurements)
        pieces.extend(str(v) for v in measurements.values() if isinstance(v, (str, bool)))
        for key in ("pattern_tags", "bullish_patterns", "bearish_patterns", "concept_tags"):
            values = measurements.get(key)
            if isinstance(values, (list, tuple, set)):
                pieces.extend(str(item) for item in values)
    return " ".join(pieces).lower()


def _contains_anchor(surface: str, anchor: str) -> bool:
    """Match a concept as a token/phrase, not as an arbitrary substring."""
    import re

    normalized_surface = re.sub(r"[^a-z0-9]+", " ", surface.lower()).strip()
    normalized_anchor = re.sub(r"[^a-z0-9]+", " ", anchor.lower()).strip()
    if not normalized_anchor:
        return False
    return f" {normalized_anchor} " in f" {normalized_surface} "


def assess_strategy_lenses(
    evidence: Iterable[Any],
    pattern_names: Iterable[str] = (),
    regime: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Assess lens relevance using independent evidence sources, never as a trade signal."""
    items = [item for item in (evidence or []) if item is not None]
    names = {str(name or "").strip().lower() for name in pattern_names if str(name or "").strip()}
    reg = {str(k).lower(): str(v).lower() for k, v in (regime or {}).items()}
    domains = {_domain(item) for item in items}
    surfaces = [_surface(item) for item in items]
    surface = " ".join([*names, *reg.values(), *surfaces])
    assessments: list[StrategyLensAssessment] = []

    for lens in STRATEGY_LENSES:
        matched = [anchor for anchor in lens.anchors if _contains_anchor(surface, anchor)]
        domain_hits = [domain for domain in lens.evidence_domains if domain in domains]
        regime_hits = [
            f"{key}={value}" for key, value in reg.items()
            if value and value not in {"unknown", "none", "balanced", "flat", "mixed"}
            and any(_contains_anchor(value, token) for token in lens.anchors)
        ]
        if matched and domain_hits:
            status = "relevant"
        elif matched or domain_hits or regime_hits:
            status = "watch"
        else:
            status = "not_evaluable"
        reasons: list[str] = []
        if matched:
            reasons.append("concept anchors: " + ", ".join(matched[:5]))
        if domain_hits:
            reasons.append("available evidence domains: " + ", ".join(domain_hits))
        if regime_hits:
            reasons.append("regime context: " + ", ".join(regime_hits[:3]))
        if not reasons:
            reasons.append("no current evidence anchors; relevance is unknown, not negative")
        source_count = len({
            str(getattr(item, "source_module", "") or type(item).__name__)
            for item in items if _domain(item) in lens.evidence_domains
        })
        if status == "relevant":
            relevance = min(1.0, 0.35 + 0.1 * min(len(matched), 3) + 0.05 * min(source_count, 3))
        elif status == "watch":
            relevance = min(0.45, 0.1 + 0.1 * bool(matched) + 0.05 * bool(domain_hits))
        else:
            relevance = 0.0
        assessments.append(StrategyLensAssessment(
            key=lens.key,
            name=lens.name,
            status=status,
            relevance=round(relevance, 3),
            reasons=tuple(reasons),
            missing_context=tuple(lens.data_requirements) if status != "relevant" else (),
            limitation=lens.limitation,
            data_requirements=tuple(lens.data_requirements),
        ))

    order = {"relevant": 0, "watch": 1, "not_evaluable": 2}
    assessments.sort(key=lambda item: (order[item.status], -item.relevance, item.name))
    return [assessment.to_dict() for assessment in assessments]

__all__ = ["StrategyLens", "StrategyLensAssessment", "STRATEGY_LENSES", "assess_strategy_lenses"]

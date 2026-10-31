"""Extensible, evidence-grounded analytical knowledge registry for Apex.

The registry describes methods Apex can reason about. It does not create trade
signals, infer unavailable data, rank a direction, or grant execution authority.
A method becomes assessable only when its declared evidence is present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class AnalyticalMethod:
    key: str
    family: str
    name: str
    domains: tuple[str, ...]
    measurements: tuple[str, ...]
    concepts: tuple[str, ...]
    market_states: tuple[str, ...]
    data_requirements: tuple[str, ...]
    limitations: str


# This is an extensible knowledge catalogue, not a list of active strategies.
# Measurement names are intentionally explicit: prose mentions do not count as
# observed data, and an unavailable feed never becomes an inferred observation.
_ROWS = (
    # Price action and structure
    ("market_structure","structure","Market structure","structure","swing_high swing_low trend_state bos choch mss","trend range transition","completed OHLC, confirmed swing anchors","Swing definitions are parameter-sensitive."),
    ("internal_external_structure","structure","Internal/external structure","structure","internal_swing external_swing structure_break","trend transition","multi-scale completed OHLC","Nested structure can conflict across horizons."),
    ("support_resistance","structure","Support and resistance","structure liquidity","support_level resistance_level level_reaction","trend range","confirmed levels and reaction history","Levels are zones with uncertainty, not guaranteed barriers."),
    ("supply_demand","structure","Supply and demand zones","structure volume","supply_zone demand_zone zone_reaction","trend range transition","zone origin, revisit and reaction data","Zone labels do not prove resting institutional orders."),
    ("trend_channels","structure","Trend channels and regression channels","structure volatility","channel_slope channel_width channel_break","trend range","sufficient completed price history","Channel fitting is sensitive to anchors and lookback."),
    ("chart_patterns","structure","Classical chart patterns","structure","head_shoulders double_top double_bottom triangle wedge flag pennant","trend range transition","confirmed pivots and completed bars","Pattern recognition is ambiguous and hindsight-sensitive."),
    ("break_retest","structure","Break and retest","structure momentum","breakout retest acceptance rejection","trend range transition","range boundary and post-break closes","A boundary touch alone does not prove acceptance."),
    ("failed_auction","structure liquidity","Failed auction and rejection","structure liquidity","failed_breakout liquidity_sweep reclaim rejection","range transition","prior extreme and post-break response","A wick alone is not a reversal thesis."),
    ("fibonacci","structure","Fibonacci retracement and extension","structure","fib_retracement fib_extension measured_move","trend transition","confirmed swing anchors","Anchor choice is subjective; levels are not independent evidence."),
    ("gap_analysis","structure volatility","Gap and discontinuity analysis","structure volatility","gap_size gap_fill gap_acceptance","transition","session boundaries and timestamped bars","Spot/CFD gaps may be broker-specific."),
    ("opening_range","session structure","Opening-range analysis","session structure volatility","opening_range_high opening_range_low opening_range_break","intraday trend range","verified session calendar and opening bars","Session boundaries vary by venue and DST."),
    ("candlestick_patterns","price_action","Candlestick patterns","structure momentum","engulfing pin_bar doji inside_bar outside_bar hammer shooting_star","all","completed OHLC candles and context","Patterns alone do not establish edge."),
    ("candle_microstructure","price_action","Candle anatomy and sequences","structure momentum volatility","body_ratio upper_wick_ratio lower_wick_ratio close_location consecutive_closes","all","completed OHLC candles","Forming candles are provisional and may change."),
    # ICT / SMC / liquidity
    ("ict_smc","institutional_price_action","ICT / Smart Money Concepts","structure liquidity","fvg order_block breaker_block inducement displacement bos choch","trend range transition","completed candles and confirmed structural levels","Named concepts are price hypotheses, not proof of institutional intent."),
    ("liquidity_pools","liquidity","Buy-side and sell-side liquidity","liquidity structure","equal_highs equal_lows swing_liquidity","all","confirmed highs/lows and instrument tick size","Stop locations are inferred, not directly observed."),
    ("liquidity_sweep","liquidity","Liquidity sweep / raid","liquidity structure","liquidity_sweep sweep_reclaim swept_level","range transition","prior liquidity level and completed reaction","A sweep without reaction may be continuation."),
    ("inducement","liquidity","Inducement and stop-run hypotheses","liquidity structure","inducement stop_hunt turtle_soup failed_breakout","range transition","confirmed local extremes and post-sweep response","Intent cannot be inferred from price alone."),
    ("fair_value_gap","institutional_price_action","Fair Value Gap and imbalance","structure liquidity","fvg fvg_fill fvg_mitigation inverse_fvg","trend transition","three-candle completed sequence","Gap presence does not imply a future fill or reaction."),
    ("order_blocks","institutional_price_action","Order blocks and mitigation","structure liquidity","order_block breaker_block mitigation_block block_state","trend range transition","impulse origin, mitigation and break history","Institutional ownership is not observable from OHLC."),
    ("dealing_range","institutional_price_action","Dealing range, premium and discount","structure","dealing_range premium discount equilibrium consequent_encroachment","trend range","explicit anchor range","Anchor selection can change interpretation."),
    ("ote","institutional_price_action","Optimal Trade Entry geometry","structure","ote_zone fib_retracement","trend transition","confirmed impulse anchors","Geometric overlap is not independent confirmation."),
    ("balanced_price_range","institutional_price_action","Balanced Price Range","structure liquidity","bpr overlapping_fvg","trend range","time-aligned opposing imbalances","Overlap rules require explicit definitions."),
    ("power_of_three","institutional_price_action","Power of Three hypothesis","session liquidity structure","accumulation manipulation distribution session_range","intraday transition","session-aligned range and follow-through","Narrative labels can overfit ordinary volatility."),
    ("crt","institutional_price_action","Candle Range Theory","structure liquidity","crt_range anchor_high anchor_low sweep_reclaim","trend range","declared anchor candle and lower timeframe","Anchor and timeframe must be explicit."),
    ("smt_divergence","intermarket","SMT divergence","correlation structure","smt_divergence relative_swing_divergence","trend transition","synchronized related-instrument bars","Correlation instability can invalidate the comparison."),
    ("wyckoff","market_cycle","Wyckoff phases and events","structure volume liquidity","wyckoff_phase spring upthrust sos sow lps lpsy","range transition","range, participation and follow-through","Phase labels are interpretive and require validation."),
    ("wyckoff_effort_result","market_cycle","Wyckoff effort versus result","volume structure","effort_result volume_price_progress absorption","range trend","comparable volume history","Tick volume is not consolidated exchange volume."),
    # Indicators
    ("moving_averages","trend_indicators","SMA EMA WMA HMA VWMA","momentum structure","sma ema wma hma vwma ema_slope ema_spread_atr ma_cross","trend range","historical closes and declared periods","Lagging measures whipsaw in ranges."),
    ("rsi","momentum_indicators","RSI and RSI variants","momentum","rsi rsi_fast rsi_slow rsi_divergence","trend range transition","close series and period definition","Overbought/oversold does not imply reversal."),
    ("macd","momentum_indicators","MACD","momentum","macd macd_signal macd_histogram macd_cross","trend transition","close series and EMA parameters","Lagging and parameter-sensitive."),
    ("stochastic","momentum_indicators","Stochastic and Stochastic RSI","momentum","stochastic stochastic_k stochastic_d stoch_rsi","range trend","high low close series","Oscillators can remain extreme in trends."),
    ("cci_williams","momentum_indicators","CCI and Williams %R","momentum","cci williams_r","range trend","OHLC series and periods","Thresholds are not universal."),
    ("roc_momentum","momentum_indicators","ROC and momentum","momentum","roc momentum_rate momentum_acceleration","trend transition","time-aligned close history","Momentum can be distorted by gaps."),
    ("adx_directional","trend_indicators","ADX and directional movement","momentum volatility","adx plus_di minus_di directional_strength","trend range","OHLC history","Strength is not direction."),
    ("aroon_trix","trend_indicators","Aroon and TRIX","momentum structure","aroon_up aroon_down trix","trend transition","OHLC/close history","Lag and lookback sensitivity apply."),
    ("atr_true_range","volatility_indicators","ATR and true range","volatility","atr true_range atr_percentile","all","OHLC and instrument price scale","ATR measures movement, not direction."),
    ("bollinger","volatility_indicators","Bollinger Bands","volatility","bollinger_upper bollinger_lower bollinger_width bollinger_percent_b","range transition","close history and standard deviation convention","Band touches are not standalone reversal evidence."),
    ("keltner_donchian","volatility_indicators","Keltner and Donchian Channels","volatility structure","keltner_upper keltner_lower donchian_high donchian_low","trend breakout","OHLC and ATR/EMA inputs","Breakouts can fail."),
    ("realized_volatility","volatility_indicators","Realized and Parkinson volatility","volatility","realized_volatility parkinson_volatility volatility_ratio","all","timestamped returns or OHLC","Estimator assumptions differ by sampling frequency."),
    ("zscore_mean","statistical_indicators","Rolling Z-score and mean displacement","momentum volatility","rolling_zscore statistical_displacement","range transition","rolling distribution and stationarity checks","Extremes can persist; mean reversion is not guaranteed."),
    ("divergence","momentum_indicators","Price / oscillator divergence","momentum structure","rsi_divergence macd_divergence momentum_divergence","trend transition","confirmed price pivots and oscillator series","Divergence can persist for long periods."),
    ("volume_indicators","volume_indicators","OBV MFI CMF A/D","volume momentum","obv mfi chaikin_money_flow accumulation_distribution","trend range","volume series with source classification","Feed volume semantics must be known."),
    ("vwap","volume_auction","VWAP and anchored VWAP","volume structure","vwap anchored_vwap vwap_deviation","intraday trend range","reliable volume or explicitly labeled proxy","Tick-volume VWAP is not centralized market VWAP."),
    ("pivot_points","price_location","Pivot points","structure","pivot_point pivot_support pivot_resistance","intraday range","prior session OHLC and session definition","Formula variants differ."),
    # Volume / auction / microstructure
    ("volume_profile","volume_auction","Volume Profile","volume structure","poc vah val hvn lvn volume_profile","range trend","volume-at-price or explicit proxy","OHLC alone cannot reconstruct true volume-at-price."),
    ("delta_cvd","order_flow","Bid/ask delta and CVD","order_flow volume","delta cvd cumulative_delta","trend reversal","aggressor-side or bid/ask trade classification","Not available from many retail feeds."),
    ("footprint","order_flow","Footprint and imbalance","order_flow volume","footprint_imbalance bid_ask_imbalance","all","trade-level bid/ask volume","Requires suitable exchange/trade data."),
    ("absorption_exhaustion","order_flow","Absorption and exhaustion","order_flow liquidity","absorption exhaustion replenishment depletion","range transition","depth/trade flow or labeled proxy","Proxy evidence must not be presented as direct order-book fact."),
    ("order_book","microstructure","Order-book depth and imbalance","order_flow execution_quality","order_book_imbalance depth spread queue_imbalance","all","timestamped depth snapshots","Depth may be fragmented, spoofed or unavailable."),
    ("tick_microstructure","microstructure","Tick velocity and quote dynamics","momentum execution_quality","tick_rate quote_rate spread_change microprice","all","fresh timestamped ticks and quotes","Polling limits observable event frequency."),
    ("market_impact","microstructure","Market impact and adverse selection","execution_quality order_flow","market_impact adverse_selection fill_probability","all","broker fills, quotes and order metadata","Requires instrument/account-specific observations."),
    # Regime / strategy families
    ("trend_following","strategy","Trend following and pullback continuation","structure momentum volatility","trend_state pullback_depth trend_efficiency","trend","multi-horizon completed prices","Trend definitions and costs must be tested."),
    ("momentum_breakout","strategy","Momentum and volatility breakout","momentum volatility structure","momentum_acceleration volatility_expansion breakout_acceptance","trend breakout","range, volatility baseline and follow-through","Expansion is not inherently directional."),
    ("mean_reversion","strategy","Mean reversion and statistical reversion","momentum volatility structure","rolling_zscore mean_distance trend_strength","range","rolling distribution and stationarity evidence","Strong trends can overwhelm reversion."),
    ("range_rotation","strategy","Range rotation and auction balance","structure volume volatility","range_rotation value_area poc vwap","range","stable range and session/volume context","Range can break without warning."),
    ("event_driven","strategy","Event-driven repricing","macro volatility execution_quality","event_surprise event_time event_window volatility_jump","event transition","verified event source and market reaction","Headlines do not determine direction."),
    ("relative_value","strategy","Pairs, cointegration and relative value","correlation historical_analogue","cointegration spread_zscore beta residual","relative value","synchronized histories and stability tests","Correlation is not cointegration."),
    ("seasonality","strategy","Seasonality and calendar effects","session historical_analogue","seasonality day_of_week time_of_day calendar_effect","intraday calendar","multi-period timestamped history and holdout","Multiple testing creates false discoveries."),
    ("carry","strategy","Carry, funding and financing","macro execution_quality","swap funding_rate financing_rate rollover_cost","multi-session","verified broker contract financing","Rates differ by broker and instrument."),
    ("derivatives","instrument_mechanics","Futures and options context","macro volatility execution_quality","expiry days_to_expiry implied_volatility iv_skew delta gamma vega open_interest","derivatives","authoritative contract and options-chain data","Do not infer Greeks or open interest from spot OHLC."),
    ("crypto_microstructure","instrument_mechanics","Crypto funding and liquidation context","order_flow execution_quality","funding_rate liquidation_volume open_interest exchange_basis","crypto","venue-specific derivatives data","Exchange fragmentation and venue risk apply."),
    ("synthetic_market","instrument_mechanics","Synthetic instrument mechanics","execution_quality volatility","provider_regime synthetic_contract_spec quote_quality","synthetic","provider documentation and verified feed metadata","Do not invent external macro drivers for synthetic prices."),
    # Statistical / learning
    ("bayesian","quantitative","Bayesian updating","historical_analogue reasoning","prior likelihood posterior credible_interval","all","defined prior and point-in-time observations","Posterior quality depends on model and data assumptions."),
    ("time_series","quantitative","Time-series dependence and stationarity","historical_analogue volatility","autocorrelation stationarity unit_root","all","sufficient ordered observations","Non-stationarity breaks many estimators."),
    ("regime_switching","quantitative","Regime switching and change points","volatility historical_analogue","change_point regime_probability transition_probability","transition","long history and point-in-time features","Regime estimates can lag and be revised."),
    ("clustering","quantitative","Clustering and market-state discovery","historical_analogue volatility","cluster_id cluster_distance state_embedding","all","scaled features and stable training population","Clusters are descriptive until validated."),
    ("anomaly_detection","quantitative","Anomaly and novel pattern discovery","historical_analogue momentum volatility","anomaly_score novelty_score residual_outlier","all","point-in-time feature store and multiple-test controls","Discovery is not predictive validation."),
    ("monte_carlo","quantitative","Bootstrap and Monte Carlo stress","historical_analogue portfolio","bootstrap_interval monte_carlo_drawdown tail_quantile","all","validated return/risk distributions","Simulation cannot repair biased input data."),
    ("calibration","quantitative","Probability calibration and validation","historical_analogue reasoning","brier_score reliability_gap calibration_sample_count","all","selection-aware labels, walk-forward and untouched holdout","Self-declared flags are not producer attestation."),
    # Macro / intermarket / instrument
    ("macro_rates","macro","Rates, inflation and central-bank policy","macro volatility","policy_rate real_yield yield_curve inflation_surprise","macro event","timestamped authoritative macro series","Expectations and actual releases must be separated."),
    ("macro_growth","macro","Growth and employment releases","macro volatility","gdp_surprise cpi_surprise pce_surprise nfp_surprise pmi_surprise","macro event","verified calendar, consensus and actual release","Consensus sources and revisions matter."),
    ("cross_asset","intermarket","Cross-asset relationships","correlation macro","rolling_correlation beta lead_lag relative_strength","all","synchronized timestamps across instruments","Correlation does not establish causation."),
    ("currency_strength","intermarket","Currency and commodity relative strength","correlation momentum","currency_strength strength_dispersion","forex commodities","broad synchronized instrument basket","Coverage bias changes the ranking."),
    ("session_calendar","session","Session, DST and holiday context","session execution_quality","session_id session_open session_close dst_status holiday_status","intraday","authoritative venue calendar and timezone","Static UTC windows are not DST-safe."),
    ("instrument_economics","instrument_mechanics","Contract, tick, margin and trading constraints","execution_quality portfolio","contract_size tick_size tick_value volume_step stop_level margin_rate","all","broker-verified instrument specification","Unknown specifications block execution geometry."),
    ("execution_cost","execution","Spread, commission, slippage and financing","execution_quality","spread commission slippage swap latency fill_probability","all","fresh quotes and attributable broker/fill data","Fallback estimates cannot certify net expectancy."),
    ("portfolio_risk","portfolio","Portfolio concentration and risk of ruin","portfolio execution_quality","portfolio_exposure correlated_exposure margin_used expected_shortfall drawdown","all","broker-truth book and synchronized return history","Stops do not cap gap risk."),
)


def _build_catalog() -> tuple[AnalyticalMethod, ...]:
    items = []
    for row in _ROWS:
        key, family, name, domains, measurements, states, requirements, limitation = row
        items.append(AnalyticalMethod(
            key=key,
            family=family,
            name=name,
            domains=tuple(domains.split()),
            measurements=tuple(measurements.split()),
            concepts=tuple(measurements.split()),
            market_states=tuple(states.split()),
            data_requirements=tuple(requirements.split("; ")),
            limitations=limitation,
        ))
    keys = [item.key for item in items]
    if len(keys) != len(set(keys)):
        raise RuntimeError("Analytical knowledge registry contains duplicate keys")
    return tuple(items)


ANALYTICAL_KNOWLEDGE: tuple[AnalyticalMethod, ...] = _build_catalog()
KNOWLEDGE_VERSION = "1.0.0"


def _domain(value: Any) -> str:
    return str(getattr(value, "value", value) or "other").strip().lower()


def assess_analytical_knowledge(
    evidence: Iterable[Any],
    regime: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Assess method coverage strictly from supplied, current evidence.

    "relevant" means at least one declared measurement was observed in an
    appropriate domain. "watch" means the domain exists but required measurements
    do not. "not_assessable" means no suitable domain evidence exists.
    """
    records = list(evidence or ())
    normalized = []
    for item in records:
        measurements = getattr(item, "measurements", {})
        if not isinstance(measurements, dict):
            measurements = {}
        normalized.append({
            "id": str(getattr(item, "evidence_id", "") or ""),
            "domain": _domain(getattr(item, "domain", "other")),
            "source": str(getattr(item, "source_module", "") or ""),
            "measurements": {str(k).lower() for k in measurements},
        })
    result: list[dict[str, Any]] = []
    for method in ANALYTICAL_KNOWLEDGE:
        domain_rows = [row for row in normalized if row["domain"] in method.domains]
        matches = [
            row for row in domain_rows
            if set(method.measurements).intersection(row["measurements"])
        ]
        matched_keys = sorted({
            key for row in matches for key in set(method.measurements).intersection(row["measurements"])
        })
        sources = sorted({row["source"] for row in matches if row["source"]})
        evidence_ids = list(dict.fromkeys(row["id"] for row in matches if row["id"]))
        if matches:
            status = "relevant"
            reason = "Declared measurements are present in compatible evidence domains."
        elif domain_rows:
            status = "watch"
            reason = "Related evidence exists, but this method's declared measurements are absent."
        else:
            status = "not_assessable"
            reason = "Required evidence domains are not represented in this market snapshot."
        result.append({
            "key": method.key,
            "family": method.family,
            "name": method.name,
            "status": status,
            "matched_measurements": matched_keys,
            "evidence_ids": evidence_ids,
            "source_modules": sources,
            "domains_present": sorted({row["domain"] for row in domain_rows}),
            "market_states": list(method.market_states),
            "data_requirements": list(method.data_requirements),
            "limitation": method.limitations,
            "reason": reason,
            "authority": "non_authoritative_analysis_only",
            "knowledge_version": KNOWLEDGE_VERSION,
        })
    return result


def knowledge_coverage(assessments: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(assessments or ())
    counts: dict[str, int] = {}
    for row in rows:
        family = str(row.get("family", "unknown"))
        counts[family] = counts.get(family, 0) + 1
    return {
        "knowledge_version": KNOWLEDGE_VERSION,
        "catalogue_size": len(ANALYTICAL_KNOWLEDGE),
        "assessed": len(rows),
        "relevant": sum(row.get("status") == "relevant" for row in rows),
        "watch": sum(row.get("status") == "watch" for row in rows),
        "not_assessable": sum(row.get("status") == "not_assessable" for row in rows),
        "families": counts,
        "semantics": "coverage_only_not_signal_not_probability_not_order_authority",
    }


__all__ = [
    "AnalyticalMethod", "ANALYTICAL_KNOWLEDGE", "KNOWLEDGE_VERSION",
    "assess_analytical_knowledge", "knowledge_coverage",
]

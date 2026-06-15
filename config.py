"""
APEX TRADER — Configuration
All settings in one place. Per-instrument pip sizes, scoring thresholds,
risk parameters, and the complete instrument registry covering Forex,
commodities, indices, and Deriv synthetics.
"""

import math
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger


class InstrumentCategory(Enum):
    FOREX = "forex"
    COMMODITY = "commodity"
    INDEX = "index"
    SYNTHETIC = "synthetic"
    CRYPTO = "crypto"


class Platform(Enum):
    MT5 = "mt5"
    DERIV = "deriv"
    BOTH = "both"


class MarginCategory(Enum):
    MAJOR = "major"
    MINOR = "minor"
    EXOTIC = "exotic"
    COMMODITY = "commodity"
    INDEX = "index"
    SYNTHETIC = "synthetic"


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    name: str
    category: InstrumentCategory
    platform: Platform
    pip_size: float
    pip_value_per_lot: float
    typical_spread_pips: float
    trading_hours: str
    margin_category: MarginCategory


# ---------------------------------------------------------------------------
# Complete instrument registry — 59 instruments across 4 categories
# ---------------------------------------------------------------------------

INSTRUMENT_REGISTRY: dict[str, InstrumentInfo] = {}


def _register(
    symbol: str, name: str, category: InstrumentCategory,
    platform: Platform, pip_size: float, pip_value: float,
    spread: float, hours: str, margin: MarginCategory,
) -> None:
    INSTRUMENT_REGISTRY[symbol] = InstrumentInfo(
        symbol=symbol, name=name, category=category, platform=platform,
        pip_size=pip_size, pip_value_per_lot=pip_value,
        typical_spread_pips=spread, trading_hours=hours,
        margin_category=margin,
    )


# ── Forex majors (7) ──────────────────────────────────────────────────────
_FX = InstrumentCategory.FOREX
_B = Platform.BOTH
_MAJ = MarginCategory.MAJOR
_MIN = MarginCategory.MINOR

_register("EURUSD", "Euro / US Dollar",          _FX, _B, 0.0001, 10.0,  1.0, "24/5", _MAJ)
_register("GBPUSD", "British Pound / US Dollar",  _FX, _B, 0.0001, 10.0,  1.2, "24/5", _MAJ)
_register("USDJPY", "US Dollar / Japanese Yen",   _FX, _B, 0.01,    6.5,  1.0, "24/5", _MAJ)
_register("USDCHF", "US Dollar / Swiss Franc",    _FX, _B, 0.0001, 10.0,  1.5, "24/5", _MAJ)
_register("AUDUSD", "Australian Dollar / US Dollar", _FX, _B, 0.0001, 10.0, 1.2, "24/5", _MAJ)
_register("NZDUSD", "New Zealand Dollar / US Dollar", _FX, _B, 0.0001, 10.0, 1.5, "24/5", _MAJ)
_register("USDCAD", "US Dollar / Canadian Dollar", _FX, _B, 0.0001, 10.0, 1.4, "24/5", _MAJ)

# ── Forex crosses (21) ────────────────────────────────────────────────────
_register("EURGBP", "Euro / British Pound",         _FX, _B, 0.0001, 10.0, 1.5, "24/5", _MIN)
_register("EURJPY", "Euro / Japanese Yen",           _FX, _B, 0.01,   6.5,  1.5, "24/5", _MIN)
_register("GBPJPY", "British Pound / Japanese Yen",  _FX, _B, 0.01,   6.5,  2.5, "24/5", _MIN)
_register("AUDJPY", "Australian Dollar / Japanese Yen", _FX, _B, 0.01, 6.5, 2.0, "24/5", _MIN)
_register("NZDJPY", "NZ Dollar / Japanese Yen",      _FX, _B, 0.01,   6.5,  2.5, "24/5", _MIN)
_register("CADJPY", "Canadian Dollar / Japanese Yen", _FX, _B, 0.01,   6.5,  2.0, "24/5", _MIN)
_register("CHFJPY", "Swiss Franc / Japanese Yen",    _FX, _B, 0.01,   6.5,  2.5, "24/5", _MIN)
_register("EURCHF", "Euro / Swiss Franc",            _FX, _B, 0.0001, 10.0, 2.0, "24/5", _MIN)
_register("EURAUD", "Euro / Australian Dollar",      _FX, _B, 0.0001, 10.0, 2.0, "24/5", _MIN)
_register("EURCAD", "Euro / Canadian Dollar",        _FX, _B, 0.0001, 10.0, 2.0, "24/5", _MIN)
_register("EURNZD", "Euro / NZ Dollar",             _FX, _B, 0.0001, 10.0, 3.0, "24/5", _MIN)
_register("GBPAUD", "British Pound / Australian Dollar", _FX, _B, 0.0001, 10.0, 3.0, "24/5", _MIN)
_register("GBPCAD", "British Pound / Canadian Dollar", _FX, _B, 0.0001, 10.0, 3.0, "24/5", _MIN)
_register("GBPCHF", "British Pound / Swiss Franc",   _FX, _B, 0.0001, 10.0, 3.0, "24/5", _MIN)
_register("GBPNZD", "British Pound / NZ Dollar",     _FX, _B, 0.0001, 10.0, 4.0, "24/5", _MIN)
_register("AUDCAD", "Australian Dollar / Canadian Dollar", _FX, _B, 0.0001, 10.0, 2.5, "24/5", _MIN)
_register("AUDCHF", "Australian Dollar / Swiss Franc", _FX, _B, 0.0001, 10.0, 2.5, "24/5", _MIN)
_register("AUDNZD", "Australian Dollar / NZ Dollar", _FX, _B, 0.0001, 10.0, 2.5, "24/5", _MIN)
_register("NZDCAD", "NZ Dollar / Canadian Dollar",   _FX, _B, 0.0001, 10.0, 3.0, "24/5", _MIN)
_register("NZDCHF", "NZ Dollar / Swiss Franc",       _FX, _B, 0.0001, 10.0, 3.5, "24/5", _MIN)
_register("CADCHF", "Canadian Dollar / Swiss Franc",  _FX, _B, 0.0001, 10.0, 3.0, "24/5", _MIN)

# ── Commodities (4) ───────────────────────────────────────────────────────
_COM = InstrumentCategory.COMMODITY
_CMAR = MarginCategory.COMMODITY
_M5 = Platform.MT5

_register("XAUUSD", "Gold",       _COM, _B, 0.01,  10.0,  2.0, "24/5", _CMAR)
_register("XAGUSD", "Silver",     _COM, _B, 0.001, 50.0,  3.0, "24/5", _CMAR)
_register("XBRUSD", "Brent Crude", _COM, _M5, 0.01,  10.0, 4.0, "specific", _CMAR)
_register("XTIUSD", "WTI Crude",  _COM, _M5, 0.01,  10.0,  4.0, "specific", _CMAR)

# ── Indices (10) ──────────────────────────────────────────────────────────
_IDX = InstrumentCategory.INDEX
_IMAR = MarginCategory.INDEX

_register("US100",  "Nasdaq 100",   _IDX, _M5, 0.1, 1.0, 1.5, "specific", _IMAR)
_register("US30",   "Dow Jones 30", _IDX, _M5, 0.1, 1.0, 2.0, "specific", _IMAR)
_register("US500",  "S&P 500",      _IDX, _M5, 0.1, 1.0, 0.5, "specific", _IMAR)
_register("GER40",  "DAX 40",       _IDX, _M5, 0.1, 1.0, 1.5, "specific", _IMAR)
_register("UK100",  "FTSE 100",     _IDX, _M5, 0.1, 1.0, 2.0, "specific", _IMAR)
_register("JP225",  "Nikkei 225",   _IDX, _M5, 1.0, 0.5, 8.0, "specific", _IMAR)
_register("AUS200", "ASX 200",      _IDX, _M5, 0.1, 1.0, 3.0, "specific", _IMAR)
_register("FRA40",  "CAC 40",       _IDX, _M5, 0.1, 1.0, 2.0, "specific", _IMAR)
# ESP35 is NOT available on metaquotes_ltd virtual — kept for real-account brokers.
# Remove from enabled_symbols_override or leave it; the connector will skip it gracefully.
_register("ESP35",  "IBEX 35",      _IDX, _M5, 0.1, 1.0, 5.0, "specific", _IMAR)
_register("HK50",   "Hang Seng",    _IDX, _M5, 0.1, 1.0, 8.0, "specific", _IMAR)

# ── Deriv synthetics (17) ─────────────────────────────────────────────────
_SYN = InstrumentCategory.SYNTHETIC
_DER = Platform.DERIV
_SMAR = MarginCategory.SYNTHETIC

_register("V10_1S",    "Volatility 10 (1s)",   _SYN, _DER, 0.001, 1.0, 0.5, "24/7", _SMAR)
_register("V25_1S",    "Volatility 25 (1s)",   _SYN, _DER, 0.001, 1.0, 0.8, "24/7", _SMAR)
_register("V50_1S",    "Volatility 50 (1s)",   _SYN, _DER, 0.001, 1.0, 1.5, "24/7", _SMAR)
_register("V75_1S",    "Volatility 75 (1s)",   _SYN, _DER, 0.001, 1.0, 2.0, "24/7", _SMAR)
_register("V100_1S",   "Volatility 100 (1s)",  _SYN, _DER, 0.001, 1.0, 3.0, "24/7", _SMAR)
# _register("BOOM300", "Boom 300", _SYN, _DER, 0.01, 1.0, 1.0, "24/7", _SMAR)  # Not on virtual accounts
_register("BOOM500",   "Boom 500",             _SYN, _DER, 0.01,  1.0, 1.0, "24/7", _SMAR)
_register("BOOM1000",  "Boom 1000",            _SYN, _DER, 0.01,  1.0, 0.5, "24/7", _SMAR)
# _register("CRASH300", "Crash 300", _SYN, _DER, 0.01, 1.0, 1.0, "24/7", _SMAR)  # Not on virtual accounts
_register("CRASH500",  "Crash 500",            _SYN, _DER, 0.01,  1.0, 1.0, "24/7", _SMAR)
_register("CRASH1000", "Crash 1000",           _SYN, _DER, 0.01,  1.0, 0.5, "24/7", _SMAR)
_register("STPIDX",    "Step Index",           _SYN, _DER, 0.1,   1.0, 0.5, "24/7", _SMAR)
_register("RNGBULL",   "Range Break Bull",     _SYN, _DER, 0.01,  1.0, 1.0, "24/7", _SMAR)
_register("RNGBEAR",   "Range Break Bear",     _SYN, _DER, 0.01,  1.0, 1.0, "24/7", _SMAR)
_register("JD10",      "Jump 10",             _SYN, _DER, 0.01,  1.0, 0.5, "24/7", _SMAR)
_register("JD25",      "Jump 25",             _SYN, _DER, 0.01,  1.0, 0.5, "24/7", _SMAR)
_register("JD50",      "Jump 50",             _SYN, _DER, 0.01,  1.0, 0.5, "24/7", _SMAR)

# ── Crypto (24/7 — available on both MT5 CFD brokers and Deriv) ──────────
# Pip sizes: 1.0 USD per pip for BTC-class, 0.01 for lower-price coins.
# Typical spreads reflect CFD crypto; actual may vary by broker.
_CRY = InstrumentCategory.CRYPTO  # proper CRYPTO category — 24/7, on MT5
_CRYMAR = MarginCategory.SYNTHETIC

_register("BTCUSD",  "Bitcoin / US Dollar",      _CRY, _B, 1.0,   1.0, 50.0,  "24/7", _CRYMAR)
_register("ETHUSD",  "Ethereum / US Dollar",     _CRY, _B, 0.1,   1.0, 3.0,   "24/7", _CRYMAR)
_register("LTCUSD",  "Litecoin / US Dollar",     _CRY, _B, 0.01,  1.0, 0.5,   "24/7", _CRYMAR)
_register("XRPUSD",  "Ripple / US Dollar",       _CRY, _B, 0.0001,1.0, 0.05,  "24/7", _CRYMAR)
_register("BNBUSD",  "BNB / US Dollar",          _CRY, _B, 0.01,  1.0, 1.0,   "24/7", _CRYMAR)
_register("SOLUSD",  "Solana / US Dollar",       _CRY, _B, 0.01,  1.0, 0.5,   "24/7", _CRYMAR)
_register("ADAUSD",  "Cardano / US Dollar",      _CRY, _B, 0.0001,1.0, 0.01,  "24/7", _CRYMAR)
_register("DOTUSD",  "Polkadot / US Dollar",     _CRY, _B, 0.001, 1.0, 0.1,   "24/7", _CRYMAR)
# DOGEUSD removed — not available on MetaQuotes MT5 or Deriv (confirmed 2026-05-31)


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def get_instrument(symbol: str) -> InstrumentInfo:
    """Get instrument info by symbol. Raises KeyError if not found."""
    sym = symbol.upper().replace("/", "")
    if sym not in INSTRUMENT_REGISTRY:
        raise KeyError(f"Unknown instrument: {symbol}")
    return INSTRUMENT_REGISTRY[sym]


def get_instruments_by_category(category: str) -> list[InstrumentInfo]:
    """Return all instruments of a given category ('forex', 'commodity', 'index', 'synthetic')."""
    cat = InstrumentCategory(category.lower())
    return [i for i in INSTRUMENT_REGISTRY.values() if i.category == cat]


def get_instruments_by_platform(platform: str) -> list[InstrumentInfo]:
    """Return instruments available on a platform ('mt5', 'deriv', 'both')."""
    return [
        i for i in INSTRUMENT_REGISTRY.values()
        if i.platform == Platform(platform.lower()) or i.platform == Platform.BOTH
    ]


def get_pip_size(symbol: str) -> float:
    """Shortcut — returns the pip size for any symbol."""
    return get_instrument(symbol).pip_size


def get_all_symbols() -> list[str]:
    """All registered symbols."""
    return list(INSTRUMENT_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Scoring thresholds
# ---------------------------------------------------------------------------

@dataclass
class ScoringConfig:
    min_entry_score: int = 65
    watchlist_score: int = 55
    structure_points: int = 20
    order_block_points: int = 20
    fvg_points: int = 15
    mtf_confluence_points: int = 15
    session_points: int = 10
    news_points: int = 10
    currency_strength_points: int = 10
    ranging_score_cap: int = 85  # Must exceed DrawdownGuard score floors (65/70/75) to allow ranging trades
    volatile_score_cap: int = 100  # FIX: was 0 — killed all volatile-regime trades
    use_adaptive_scoring_weights: bool = True


# ---------------------------------------------------------------------------
# Confirmation-only penalties (VWAP, RSI/MACD, ATR percentile, VP-POC)
# ---------------------------------------------------------------------------

@dataclass
class ConfirmationPenaltyConfig:
    enabled: bool = True
    shadow_mode: bool = False
    vwap_wrong_side_penalty: int = 15
    divergence_both_tf_penalty: int = 15
    divergence_single_tf_penalty: int = 7
    atr_dead_regime_penalty: int = 10
    vp_poc_trap_penalty: int = 10
    atr_dead_percentile: float = 20.0
    atr_percentile_window: int = 100
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    vwap_min_session_minutes: int = 30
    vp_poc_lookback: int = 100
    vp_poc_proximity_pct: float = 0.5


# ---------------------------------------------------------------------------
# Directional consensus — weighted signed voting across brain modules
# ---------------------------------------------------------------------------

_DEFAULT_CONSENSUS_WEIGHTS: dict[str, float] = {
    "structure": 1.0,
    "currency_strength": 1.0,
    "wyckoff": 1.0,
    "volume": 1.0,
    "order_block": 1.0,
    "fvg": 1.0,
    "liquidity": 1.0,
    "momentum": 1.0,
    "vwap": 1.0,
}


@dataclass
class ConsensusConfig:
    enabled: bool = True
    weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_CONSENSUS_WEIGHTS))
    min_net_score: float = 1.5
    min_agreement: float = 0.55
    high_authority_modules: list[str] = field(
        default_factory=lambda: ["currency_strength"]
    )
    high_authority_oppose_confidence: float = 0.6
    min_contributors: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.min_contributors, int) or self.min_contributors < 1:
            raise ValueError(
                f"ConsensusConfig.min_contributors must be an int >= 1, "
                f"got {self.min_contributors!r}"
            )
        for name, w in self.weights.items():
            if not isinstance(w, (int, float)) or not math.isfinite(w) or w < 0:
                raise ValueError(
                    f"ConsensusConfig.weights['{name}'] must be finite >= 0, got {w!r}"
                )
        if not any(w > 0 for w in self.weights.values()):
            raise ValueError("ConsensusConfig.weights must have at least one weight > 0")
        if not (0 < self.min_agreement <= 1.0):
            raise ValueError(
                f"ConsensusConfig.min_agreement must be in (0, 1], got {self.min_agreement!r}"
            )
        if not isinstance(self.min_net_score, (int, float)) or self.min_net_score < 0:
            raise ValueError(
                f"ConsensusConfig.min_net_score must be >= 0, got {self.min_net_score!r}"
            )
        if not (0 < self.high_authority_oppose_confidence <= 1.0):
            raise ValueError(
                f"ConsensusConfig.high_authority_oppose_confidence must be in (0, 1], "
                f"got {self.high_authority_oppose_confidence!r}"
            )
        for mod in self.high_authority_modules:
            if mod not in self.weights:
                raise ValueError(
                    f"ConsensusConfig.high_authority_modules entry '{mod}' "
                    f"not present in weights: {list(self.weights.keys())}"
                )


# ---------------------------------------------------------------------------
# Layered decision — Opportunity Quality + Entry Quality gates
# ---------------------------------------------------------------------------

_DEFAULT_OQ_WEIGHTS: dict[str, float] = {
    "volatility": 1.5,
    "spread": 1.5,
    "news": 1.0,
    "session": 1.5,
    "reward_risk": 2.0,
    "historical_ev": 1.0,
    "volume_health": 1.0,
}

_DEFAULT_EQ_WEIGHTS: dict[str, float] = {
    "ob_proximity": 2.0,
    "fvg_proximity": 1.5,
    "liquidity_proximity": 1.0,
    "atr_extension": 1.5,
    "stop_quality": 2.0,
}


@dataclass
class LayeredDecisionConfig:
    enabled: bool = True
    opportunity_quality_min: float = 5.0
    entry_quality_min: float = 5.0
    oq_weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_OQ_WEIGHTS))
    eq_weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_EQ_WEIGHTS))

    def __post_init__(self) -> None:
        for name, val in [
            ("opportunity_quality_min", self.opportunity_quality_min),
            ("entry_quality_min", self.entry_quality_min),
        ]:
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                raise ValueError(
                    f"LayeredDecisionConfig.{name} must be finite, got {val!r}"
                )
            if val < 0 or val > 10:
                raise ValueError(
                    f"LayeredDecisionConfig.{name} must be in [0, 10], got {val!r}"
                )

        for label, wdict in [("oq_weights", self.oq_weights), ("eq_weights", self.eq_weights)]:
            for k, w in wdict.items():
                if not isinstance(w, (int, float)) or not math.isfinite(w) or w < 0:
                    raise ValueError(
                        f"LayeredDecisionConfig.{label}['{k}'] must be finite >= 0, got {w!r}"
                    )
            if not any(w > 0 for w in wdict.values()):
                raise ValueError(
                    f"LayeredDecisionConfig.{label} must have at least one weight > 0"
                )


# ---------------------------------------------------------------------------
# Risk parameters
# ---------------------------------------------------------------------------

# NOTE: These are conservative defaults for the initial live validation phase.
# Once the strategy has 200+ trades and shows positive expectancy, increase
# risk_per_trade_pct to 1.0, then 2.0. Never increase before proving edge.
@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.75
    max_daily_drawdown_pct: float = 3.0
    max_weekly_drawdown_pct: float = 8.0
    max_open_trades: int = 5
    max_correlated_trades: int = 2
    min_risk_reward: float = 1.8
    # How many times the instrument's typical spread we allow before rejecting.
    # 3.0 gives headroom for indices (US100 widens ~10 pts off-hours vs typical 1.5)
    # and crypto (BTCUSD spreads balloon on thin liquidity).
    # The validator uses: max_allowed = typical_spread * max_spread_multiplier
    max_spread_multiplier: float = 3.0
    ev_threshold: float = -0.1
    # Defensive losing-pattern gate: block entries whose pair/session/regime/
    # entry-type combination is a statistically-confident loser in our own
    # history (see AdaptiveOptimizer.is_losing_pattern). Only ever blocks; the
    # gate is neutral until enough trades accumulate.
    losing_pattern_block_enabled: bool = True
    tp_adjust_enabled: bool = True
    pending_orders_enabled: bool = True
    pending_max_wait_minutes: int = 30
    max_cluster_same_direction: int = 2
    allow_intentional_hedge: bool = False
    margin_guardian_enabled: bool = True
    margin_warn_pct: float = 200.0
    margin_block_entry_pct: float = 150.0
    margin_flatten_pct: float = 100.0
    # Per-account daily-loss FLATTEN cap (Tier 2 #9). When an account's combined
    # realized + unrealized daily loss breaches this %, that account's open
    # positions are flattened and the account is halted for the day. This is the
    # harder backstop above the (entry-blocking) daily_loss_cap_pct.
    daily_loss_flatten_enabled: bool = True
    daily_loss_flatten_pct: float = 5.0
    weekend_protection_enabled: bool = True
    weekend_protection_mode: str = "derisk"
    weekend_close_buffer_minutes: int = 15
    # Hour (UTC) at which the FX week is treated as closing on Friday. New FX
    # entries are blocked within weekend_close_buffer_minutes of this time.
    friday_close_hour_utc: int = 21
    micro_account_threshold_usd: float = 100.0
    deriv_min_stake_usd: float = 0.35
    # Hard ceiling on the ACTUAL fraction of the account a single trade may
    # risk when the broker minimum lot floors the position size upward.
    # On micro accounts ($5–$200) the 0.01 min lot almost always exceeds the
    # ideal risk_amount; rather than rejecting every trade, allow it as long
    # as the real risk stays within this cap. Trades whose min-lot risk would
    # exceed this (e.g. 0.01 lot of Gold on a $5 account) are still rejected.
    max_risk_pct_per_trade: float = 5.0
    backtest_starting_balance_usd: float = 10_000.0
    tp3_ladder_enabled: bool = True
    tp3_r_multiple: float = 5.0
    tp3_close_ratio: float = 0.25
    scale_in_enabled: bool = True
    scale_in_max_adds: int = 1
    scale_in_min_profit_r: float = 1.0
    scale_in_add_ratio: float = 0.5

    # ── Min-lot inflation guard ──────────────────────────────────────────
    # When True, reject an order if the broker's volume_min floors the
    # risk-engine-sized lot upward (e.g. 0.01 → 0.5).  Default OFF so
    # a WARNING is logged but the order still proceeds as today.
    reject_on_minlot_inflation: bool = False

    # ── Continuous in-trade analysis ─────────────────────────────────────
    # Re-score open instruments every cycle and exit if thesis invalidates
    continuous_analysis_enabled: bool = True
    # Score must drop below this to trigger early invalidation exit
    invalidation_score_threshold: int = 40
    # Opposing signal must score above this to trigger early exit
    opposing_signal_threshold: int = 75
    # Minimum minutes in trade before invalidation exit can fire
    invalidation_min_hold_minutes: float = 5.0

    # ── Conviction monitoring ─────────────────────────────────────────────
    # Track score trend across cycles — exit if conviction collapses
    conviction_monitoring_enabled: bool = True
    # Number of consecutive cycles with declining score before exit
    conviction_decline_cycles: int = 5
    # Minimum score drop to count as a declining cycle
    conviction_decline_min_drop: int = 5

    # ── News exit on open trades ──────────────────────────────────────────
    news_exit_enabled: bool = True
    # Minutes before high-impact news to close/tighten open trades
    news_exit_minutes_before: int = 3
    # Whether to fully close or just tighten SL to breakeven before news
    news_exit_mode: str = "close"   # "close" | "tighten"

    # ── Session close management ─────────────────────────────────────────
    session_close_enabled: bool = True
    # Close index trades N minutes before their exchange closes
    index_close_buffer_minutes: int = 15
    # Close or tighten forex trades during dead zone (00:00-02:00 UTC)
    dead_zone_management: bool = True

    # ── Dynamic SL tightening in profit ──────────────────────────────────
    dynamic_sl_tightening_enabled: bool = True
    # R-multiple at which to start tightening beyond breakeven
    dynamic_sl_tighten_at_r: float = 2.0
    # How much to tighten: new SL = current_price - (original_risk * ratio)
    dynamic_sl_tighten_ratio: float = 0.5

    # ── Portfolio heat monitoring ─────────────────────────────────────────
    portfolio_heat_enabled: bool = True
    # Max total portfolio risk % across all open trades
    max_portfolio_heat_pct: float = 2.0
    # When heat exceeds this, block new entries
    portfolio_heat_block_pct: float = 1.8

    # ── Live portfolio risk engine (M8 Phase 4a) ────────────────────────
    # Replaces the static count-based heat with true capital-at-risk.
    # Non-destructive: DEFENSIVE state freezes entries/scale-ins and
    # advances eligible positions to breakeven.  No position is ever
    # closed or reduced by this engine (Phase 4b/4c).
    portfolio_risk_engine_enabled: bool = True
    # Heat % at which the portfolio enters DEFENSIVE state
    heat_defensive_pct: float = 1.5
    # Heat % that must be sustained before returning to NORMAL
    heat_recovery_pct: float = 1.0
    # Seconds that recovery conditions must persist before exiting DEFENSIVE
    recovery_dwell_seconds: float = 120.0
    # R-multiple of favorable excursion required before a position may be
    # advanced to breakeven during DEFENSIVE state
    be_eligible_r_multiple: float = 1.0
    # Per-position cooldown between defensive stop adjustments
    defensive_action_cooldown_seconds: float = 60.0

    # ── Portfolio risk reduction (M8 Phase 4b) ──────────────────────────
    # Controlled, graduated exposure reduction.  Default OFF.
    # Requires portfolio_risk_engine_enabled=True to have any effect.
    portfolio_reduction_enabled: bool = True
    # Heat % that triggers escalation to REDUCING state (must be > heat_defensive_pct)
    heat_reduction_pct: float = 2.5
    # Seconds in DEFENSIVE before escalating to REDUCING if breach persists
    reduction_persist_seconds: float = 300.0
    # Fraction of weakest position to trim per reduction action (0.5 = 50%)
    reduction_partial_ratio: float = 0.5
    # Per-position cooldown between reduction trims
    reduction_action_cooldown_seconds: float = 120.0
    # Max number of reduction trims per hour (safety cap)
    max_reductions_per_hour: int = 4

    # ── Emergency liquidation (M8 Phase 4c) ─────────────────────────────
    # Progressive full-close of weakest positions when portfolio survival
    # is at risk.  Default OFF.  Requires portfolio_risk_engine_enabled=True.
    # Precedence: margin_guardian (margin) >= EMERGENCY (survival) > REDUCING.
    portfolio_emergency_enabled: bool = True
    # Heat % that triggers EMERGENCY state (must be > heat_reduction_pct)
    heat_emergency_pct: float = 4.0
    # Seconds since last successful reconcile before triggering EMERGENCY
    emergency_reconcile_failure_seconds: float = 300.0
    # Absolute count divergence between managed and broker positions
    # that triggers EMERGENCY (e.g. 2 = tolerate up to 2 position mismatch)
    emergency_broker_exposure_tolerance: int = 2
    # Max positions fully closed per loop cycle in EMERGENCY
    emergency_max_closes_per_cycle: int = 1
    # Per-position cooldown between emergency closes
    emergency_action_cooldown_seconds: float = 30.0
    # Max emergency closes per hour (global circuit breaker)
    emergency_max_closes_per_hour: int = 6

    # ── Reconciliation confirmation ─────────────────────────────────────
    # Number of consecutive confirmed-absent cycles before emitting a
    # CRITICAL warning about a persistently unverifiable position.
    # The position is NEVER auto-closed — only flagged for human review.
    reconcile_max_unconfirmed_cycles: int = 20

    # ── Swap / rollover financing model (F2 Phase 1: observability) ─────
    # When enabled, estimates overnight financing from a user-supplied
    # rate table and journals the result alongside each closed trade.
    # Does NOT alter pnl_dollars, sizing, scoring, or any exit decision.
    model_swap_costs: bool = True
    swap_rates_path: str = "data/swap_rates.json"
    swap_rollover_hour_utc: int = 21
    swap_triple_weekday: int = 2  # Wednesday (Mon=0)

    # ── Volatility stop model (F1 Phase A: offline evidence only) ────────
    # Controls ATR-based stop-loss distance computation for offline
    # ATR-based volatility stop.  "off" = live SL uses structure only
    # (byte-for-byte identical to pre-PR-B behavior).  "on" = live SL
    # is replaced by a clamped ATR stop when ATR data is available,
    # falling back to the structure SL otherwise.
    volatility_stop_mode: str = "on"
    atr_stop_period: int = 14
    atr_stop_mult: float = 1.5
    # P0 fix: floor at 1.0 so the ATR stop can never tighten the structural SL
    # inside the entry zone's noise band (0.5 placed stops mid-zone → premature stop-outs).
    atr_stop_ratio_min: float = 1.0
    atr_stop_ratio_max: float = 2.0
    atr_stop_max_risk_mult: float = 4.0

    # ── Opportunity-cost exit (F4) ────────────────────────────────────
    # Detects when a better setup is blocked because all position slots
    # are full and the weakest held position is stagnant.  Shadow mode
    # only LOGS the would-fire event; it NEVER closes a position.
    # "active" closes the position via the soft-close idiom.
    opportunity_cost_exit_mode: str = "active"  # "off" | "shadow" | "active"
    opportunity_cost_score_margin: float = 10.0
    opportunity_cost_max_pnl_pips: float = 5.0
    opportunity_cost_min_hold_minutes: float = 20.0

    # ── Tick freshness guard ─────────────────────────────────────────────
    # Maximum acceptable age (seconds) of a tick before get_price rejects
    # it as stale.  Generous default (120 s) tolerates quiet/illiquid
    # instruments; override via this field or MAX_TICK_AGE_SECONDS env var.
    max_tick_age_seconds: float = 120.0

    # ── Spread / slippage deterioration monitoring ────────────────────────
    spread_monitor_enabled: bool = True
    # If spread widens beyond N× normal for this instrument, tighten SL
    spread_deterioration_multiplier: float = 3.0

    # ── HTF candle close reassessment ────────────────────────────────────
    htf_reassessment_enabled: bool = True
    # Check H1 candle close direction — if against trade, exit
    htf_reassess_on_h1_close: bool = True

    # ── H4 bias handling at entry ─────────────────────────────────────────
    # H4 is the slowest timeframe and reacts to reversals LAST, so by default
    # it acts as CONTEXT, not a dictator: a counter-H4 trade pays a score
    # penalty rather than being vetoed, letting strong M5/M1 setups through
    # (they then size DOWN via the decision engine's conviction model, which
    # already weights HTF alignment). Modes:
    #   "penalty" (default) — subtract h4_counter_trend_penalty from the score
    #   "veto"              — hard-reject counter-H4 entries (legacy behaviour)
    #   "off"               — ignore H4 entirely
    # h4_bias_gate_enabled is the master switch (False disables the gate).
    h4_bias_gate_enabled: bool = True
    h4_bias_gate_mode: str = "penalty"
    h4_counter_trend_penalty: int = 15
    # Regime score-threshold gate: when the RegimeLearner wants a higher
    # conviction bar for a regime, a sub-bar setup is SIZED DOWN (bounded
    # context with influence) rather than vetoed. "veto" restores the legacy
    # hard rejection. regime_below_threshold_size_mult is the size haircut.
    regime_score_threshold_mode: str = "penalty"
    regime_below_threshold_size_mult: float = 0.7

    # ── Execution-quality size throttle ────────────────────────────────
    # When enabled, degraded execution quality (high slippage/latency/spread)
    # reduces position size via get_size_multiplier. Live on demo so the grade
    # thresholds get validated/tuned against real fills; it only ever REDUCES
    # size (never increases), so it's safe to run while tuning. Set False to
    # disable.
    execution_quality_sizing_enabled: bool = True

    # ── P5: per-pair cooldown after a breakeven stop-out (minutes) ────────
    # Breaks the enter→BE→stopped-at-BE→re-enter chop loop that bleeds spread.
    # 0 disables the cooldown.
    be_stop_cooldown_minutes: float = 30.0

    # ── P8: max order slippage / deviation (points) sent to the broker ────
    # Caps how far the fill price may deviate from the requested price on
    # market orders, protecting against arbitrarily bad fills in fast markets.
    max_deviation_points: int = 20
    # P8b: per-instrument max slippage (in pips) for market entries. Converted
    # to broker points using each symbol's own point size, so the cap means the
    # same thing on FX / metals / indices / crypto (a single global point value
    # does not). 0 disables → falls back to the global max_deviation_points.
    max_slippage_pips: float = 2.0

    # ── P12: minimum profit (in R) before breakeven activates ─────────────
    # Activating BE on the first profitable tick after TP1 kills runners on a
    # normal retest. Require this much profit first so the trade can breathe.
    breakeven_min_profit_r: float = 0.5

    # ── P13: trailing-stop swing lookback (M5 bars) ───────────────────────
    # A 3-bar lookback trails on intracandle noise (15min). 12 bars ≈ 1h of
    # M5 structure, giving runners room to reach H1 swing targets.
    trailing_swing_lookback: int = 12

    # ── Absolute / early profit protection ───────────────────────────────
    # All R-gated breakeven logic (TP1 partial, breakeven_min_profit_r) needs
    # a known original risk to compute an R-multiple. Adopted/orphan trades
    # carry entry_type="ORPHAN_ADOPTED" with no reliable original risk, so the
    # R-gates NEVER fire — a position can run +$X then round-trip into a loss
    # with nothing protecting it. This locks SL to (near) breakeven the moment
    # open profit crosses an *absolute* floor in account currency OR pips,
    # independent of TP1 / R-multiple. Either floor triggers (whichever first);
    # set a floor to 0 to disable that leg. Fully reversible via the enable flag.
    absolute_be_protection_enabled: bool = True
    absolute_be_floor_usd: float = 15.0    # lock to BE once open profit ≥ this (account ccy)
    absolute_be_floor_pips: float = 12.0   # ...or once open profit ≥ this many pips
    absolute_be_buffer_pips: float = 1.0   # park SL this far past entry to cover costs/spread

    def __post_init__(self) -> None:
        def _check_finite_positive(name: str, val: float) -> None:
            if not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
                raise ValueError(
                    f"RiskConfig.{name} must be a finite number > 0, got {val!r}"
                )

        def _check_finite_non_negative(name: str, val: float) -> None:
            if not isinstance(val, (int, float)) or not math.isfinite(val) or val < 0:
                raise ValueError(
                    f"RiskConfig.{name} must be a finite number >= 0, got {val!r}"
                )

        _check_finite_positive("risk_per_trade_pct", self.risk_per_trade_pct)
        if self.risk_per_trade_pct >= 100:
            raise ValueError(
                f"RiskConfig.risk_per_trade_pct must be < 100, got {self.risk_per_trade_pct!r}"
            )
        _check_finite_positive("max_daily_drawdown_pct", self.max_daily_drawdown_pct)
        _check_finite_positive("max_weekly_drawdown_pct", self.max_weekly_drawdown_pct)
        _check_finite_positive("min_risk_reward", self.min_risk_reward)
        _check_finite_positive("max_spread_multiplier", self.max_spread_multiplier)
        _check_finite_positive("deriv_min_stake_usd", self.deriv_min_stake_usd)
        _check_finite_positive("backtest_starting_balance_usd", self.backtest_starting_balance_usd)
        _check_finite_positive("margin_warn_pct", self.margin_warn_pct)
        _check_finite_positive("margin_block_entry_pct", self.margin_block_entry_pct)
        _check_finite_positive("margin_flatten_pct", self.margin_flatten_pct)
        _check_finite_non_negative("micro_account_threshold_usd", self.micro_account_threshold_usd)
        _check_finite_non_negative("absolute_be_floor_usd", self.absolute_be_floor_usd)
        _check_finite_non_negative("absolute_be_floor_pips", self.absolute_be_floor_pips)
        _check_finite_non_negative("absolute_be_buffer_pips", self.absolute_be_buffer_pips)

        if not isinstance(self.max_open_trades, int) or self.max_open_trades < 1:
            raise ValueError(
                f"RiskConfig.max_open_trades must be an int >= 1, got {self.max_open_trades!r}"
            )
        if not isinstance(self.max_correlated_trades, int) or self.max_correlated_trades < 0:
            raise ValueError(
                f"RiskConfig.max_correlated_trades must be an int >= 0, got {self.max_correlated_trades!r}"
            )

        if self.max_weekly_drawdown_pct < self.max_daily_drawdown_pct:
            raise ValueError(
                f"RiskConfig.max_weekly_drawdown_pct ({self.max_weekly_drawdown_pct}) "
                f"must be >= max_daily_drawdown_pct ({self.max_daily_drawdown_pct})"
            )
        if not (self.margin_warn_pct >= self.margin_block_entry_pct >= self.margin_flatten_pct):
            raise ValueError(
                f"RiskConfig margin levels must be descending: "
                f"margin_warn_pct ({self.margin_warn_pct}) >= "
                f"margin_block_entry_pct ({self.margin_block_entry_pct}) >= "
                f"margin_flatten_pct ({self.margin_flatten_pct})"
            )


# ---------------------------------------------------------------------------
# Decision Intelligence System
# ---------------------------------------------------------------------------

@dataclass
class DecisionConfig:
    enabled: bool = True
    journal_enabled: bool = True
    journal_dir: str = "data/decision_journal"
    governor_enabled: bool = True
    adopted_observation_minutes: float = 10.0
    # ── M5-primary decision weights (roadmap C) ───────────────────────────
    # APEX is an M5/M1 opportunity-capture system, so M5 structure quality and
    # M1 momentum CARRY the entry decision and trade SIZE; HTF (D1/H4/H1)
    # alignment is context, not the dictator. Conviction weights (≈ sum 1.0)
    # map conviction → size multiplier (0.5–1.5×). Previously HTF dominated at
    # 0.40; it is now demoted to 0.20 with M5/M1 taking the lead.
    conviction_htf_weight: float = 0.20        # was 0.40
    conviction_structure_weight: float = 0.40  # M5 zone quality (was 0.30)
    conviction_momentum_weight: float = 0.30   # M1 momentum (was 0.20)
    conviction_confidence_weight: float = 0.10
    # ENTER/SKIP scoring coefficients — HTF demoted, M5/M1 promoted.
    enter_htf_coeff: float = 0.20        # was 0.35
    enter_structure_coeff: float = 0.45  # M5 (was 0.40)
    enter_momentum_coeff: float = 0.30   # M1 (was 0.15)
    skip_htf_coeff: float = 0.20         # counter-HTF no longer dominates SKIP (was 0.35)
    skip_momentum_coeff: float = 0.30    # opposing M1 matters more (was 0.20)
    # ── Regime-dependent weighting (roadmap D) ────────────────────────────
    # In ranging/reversal regimes, shift decision influence off the slow HTF
    # and onto M1 momentum (HTF reacts last). Trending regimes keep the base
    # M5-primary weights above. regime_ranging_htf_scale=0.5 halves the HTF
    # weights and reallocates the freed conviction weight to M1 momentum.
    regime_weighting_enabled: bool = True
    regime_ranging_htf_scale: float = 0.5
    # ── Reversal trade type (roadmap E) ───────────────────────────────────
    # A counter-HTF entry is taken as a REVERSAL only when it carries strong
    # lower-timeframe evidence — M5 sweep + M1 BOS + momentum (all required by
    # default) — and such trades are sized down. Counter-trend setups WITHOUT
    # that evidence are pushed toward SKIP (falling-knife guard).
    reversal_trades_enabled: bool = True
    reversal_min_momentum: float = 0.2
    reversal_required_evidence: int = 3
    reversal_size_multiplier: float = 0.7   # counter-trend reversals run at −30% size
    reversal_no_evidence_skip_penalty: float = 0.30
    # ── HTF = bounded context (Scenario A) ────────────────────────────────
    # When the full HTF stack (D1+H4+H1) supports the trade direction, give a
    # bounded size BONUS on top of the conviction model — HTF helps when it
    # agrees, hurts (reversal haircut) when it opposes, but never dictates.
    htf_aligned_size_bonus: float = 0.15     # +15% size on a fully-aligned stack
    htf_aligned_threshold: float = 0.5       # min tf_alignment to count as "aligned"
    # ── Thesis-deterioration secure (roadmap G) ───────────────────────────
    # The exit brain's profit-securing actions (MOVE_TO_BREAKEVEN, TIGHTEN_SL)
    # are all gated on profit_state (an R-multiple). Adopted/orphan trades carry
    # a *reconstructed* risk, so their R is detached from real economic profit —
    # a winner can run +$X (meaningful cash) while profit_state ≈ 0.2R, below
    # every R-gate, and HOLD wins by default because nothing else can score.
    # This layer asks the trader's question instead — "is the reason I'm holding
    # still valid?" — and secures profit when MEANINGFUL ECONOMIC PROFIT exists
    # AND the thesis is deteriorating, UNLESS it's a healthy pullback in an
    # intact trend. It overrides only a would-be default HOLD; a stronger CLOSE/
    # TIGHTEN/BE verdict from the normal scoring still takes precedence.
    thesis_secure_enabled: bool = True
    thesis_secure_min_profit_usd: float = 15.0   # economic-profit trigger (account ccy)
    thesis_secure_min_profit_pips: float = 12.0  # ...or this many pips (whichever first)
    thesis_deterioration_threshold: float = 0.35  # 0..1 decay score needed to act
    # When decay is SEVERE (most dimensions collapsing at once), securing the
    # stop and waiting to be stopped just gives the move back — bank the profit
    # at market instead. Hard-close once deterioration ≥ this (and profit
    # exists). Must be > thesis_deterioration_threshold; set ≥ 1.01 to disable
    # the hard-close tier and keep only stop-securing.
    thesis_close_threshold: float = 0.80
    thesis_healthy_structure: float = 0.5    # structure ≥ this AND momentum ≥ healthy → hold (pullback)
    thesis_healthy_momentum: float = 0.0
    thesis_lock_fraction: float = 0.5        # lock this fraction of open profit into the stop
    thesis_struct_ref: float = 0.6           # structure below this starts contributing to decay
    thesis_conviction_cycles: int = 3        # re-score window for conviction-collapse detection
    thesis_conviction_drop: float = 10.0     # min scan-score drop over window to count as collapse
    thesis_conviction_full_drop: float = 30.0  # drop giving the conviction leg full weight


# ---------------------------------------------------------------------------
# Data backup — push irreplaceable runtime data to GitHub
# ---------------------------------------------------------------------------

@dataclass
class DataBackupConfig:
    enabled: bool = True
    interval_hours: int = 1
    max_file_size_mb: float = 95.0
    exclude_patterns: list[str] = field(default_factory=lambda: ["*.csv"])


# ---------------------------------------------------------------------------
# Application config
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Trade Planner — the coordinator between advisors and execution.
# The tunable parameters themselves live in planning.trade_planner.PlannerConfig
# (self-contained + serialisable so calibration persists). It is imported here
# so the planner is configured through the single AppConfig like everything else.
# ---------------------------------------------------------------------------

from planning.trade_planner import PlannerConfig  # noqa: E402  (leaf import, no cycle)

# ---------------------------------------------------------------------------
# Portfolio Governor — portfolio-level risk limits (advisory to the planner).
# The dataclass lives in governor.models (leaf, no config import) so it is
# imported here to keep all configuration under the single AppConfig.
# ---------------------------------------------------------------------------

from governor.models import GovernorConfig  # noqa: E402  (leaf import, no cycle)


@dataclass
class AppConfig:
    # All 4 categories enabled — forex, commodity, index, synthetic
    enabled_categories: list[str] = field(
        default_factory=lambda: ["forex", "commodity", "index", "synthetic", "crypto"]
    )
    # Empty by default — scans ALL instruments in enabled_categories.
    # Populate this ONLY to restrict to a subset during testing.
    # e.g. ["EURUSD", "GBPUSD"] for a quick smoke test.
    enabled_symbols_override: list[str] = field(
        default_factory=list  # <-- FIX: was hardcoded 8 pairs, now empty = scan everything
    )
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    confirmation_penalties: ConfirmationPenaltyConfig = field(default_factory=ConfirmationPenaltyConfig)
    consensus: ConsensusConfig = field(default_factory=ConsensusConfig)
    layered_decision: LayeredDecisionConfig = field(default_factory=LayeredDecisionConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    data_backup: DataBackupConfig = field(default_factory=DataBackupConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    governor: GovernorConfig = field(default_factory=GovernorConfig)
    scan_interval_seconds: int = 10
    max_consecutive_cycle_failures: int = 5
    log_level: str = "INFO"

    @property
    def enabled_pairs(self) -> list[str]:
        """All symbols from enabled categories, filtered by override if set."""
        if self.enabled_symbols_override:
            return self.enabled_symbols_override
        symbols: list[str] = []
        for cat in self.enabled_categories:
            symbols.extend(i.symbol for i in get_instruments_by_category(cat))
        return symbols

    @property
    def total_instruments(self) -> int:
        return len(self.enabled_pairs)


# ---------------------------------------------------------------------------
# Instrument behaviour helpers — used by scanner, engine, validator, loop
# These replace all hardcoded category checks so every instrument is treated
# according to its own declared properties, not by guessing from category name.
# ---------------------------------------------------------------------------

def is_session_gated(symbol: str) -> bool:
    """True if the instrument should only trade during specific FX sessions.
    Forex pairs are session-gated (London, NY). Anything else trades freely
    according to its own hours and should not be penalised by FX session gates."""
    try:
        info = get_instrument(symbol)
        return info.category == InstrumentCategory.FOREX
    except KeyError as exc:
        logger.debug("[config] instrument lookup failed for symbol, treating as forex: {}", exc)
        return True  # unknown instrument — treat conservatively as FX


def get_trading_hours(symbol: str) -> str:
    """Return the trading_hours string from the registry: '24/5', '24/7', or 'specific'."""
    try:
        return get_instrument(symbol).trading_hours
    except KeyError:
        return "24/5"


def is_always_open(symbol: str) -> bool:
    """True if the instrument trades 24/7 and should never be skipped on weekends.
    Only Deriv synthetics qualify — forex, commodities and indices are all 24/5
    and are closed on weekends."""
    try:
        info = get_instrument(symbol)
        return info.trading_hours == "24/7"
    except KeyError as exc:
        logger.debug("[config] instrument lookup failed for symbol, treating as non-24/7: {}", exc)
        return False


def session_score_floor(symbol: str) -> int:
    """Minimum session score to apply for this instrument.
    Non-FX instruments get a floor of 4 (earns partial session points) so
    Asian/Transition hours don't zero out a valid commodity or synthetic setup."""
    return 0 if is_session_gated(symbol) else 4


def spread_open_guard_applies(symbol: str) -> bool:
    """True if the instrument is subject to the London/NY open spread spike guard.
    Only applies to FX pairs — commodity and index spreads don't spike at FX opens."""
    return is_session_gated(symbol)

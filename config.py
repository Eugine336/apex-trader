"""
APEX TRADER — Configuration
All settings in one place. Per-instrument pip sizes, scoring thresholds,
risk parameters, and the complete instrument registry covering Forex,
commodities, indices, and Deriv synthetics.
"""

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
    min_entry_score: int = 70
    watchlist_score: int = 55
    structure_points: int = 20
    order_block_points: int = 20
    fvg_points: int = 15
    mtf_confluence_points: int = 15
    session_points: int = 10
    news_points: int = 10
    currency_strength_points: int = 0
    ranging_score_cap: int = 50  # Tighter cap — ranging pairs need stronger confluences
    volatile_score_cap: int = 100  # FIX: was 0 — killed all volatile-regime trades
    use_adaptive_scoring_weights: bool = True


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
    max_open_trades: int = 3
    max_correlated_trades: int = 1
    min_risk_reward: float = 1.8
    # How many times the instrument's typical spread we allow before rejecting.
    # 3.0 gives headroom for indices (US100 widens ~10 pts off-hours vs typical 1.5)
    # and crypto (BTCUSD spreads balloon on thin liquidity).
    # The validator uses: max_allowed = typical_spread * max_spread_multiplier
    max_spread_multiplier: float = 3.0
    ev_threshold: float = -0.1
    tp_adjust_enabled: bool = True
    pending_orders_enabled: bool = True
    pending_max_wait_minutes: int = 30
    max_cluster_same_direction: int = 2
    allow_intentional_hedge: bool = False
    margin_guardian_enabled: bool = False
    margin_warn_pct: float = 200.0
    margin_block_entry_pct: float = 150.0
    margin_flatten_pct: float = 100.0
    weekend_protection_enabled: bool = True
    weekend_protection_mode: str = "derisk"
    weekend_close_buffer_minutes: int = 15
    tp3_ladder_enabled: bool = True
    tp3_r_multiple: float = 5.0
    tp3_close_ratio: float = 0.25
    scale_in_enabled: bool = False
    scale_in_max_adds: int = 1
    scale_in_min_profit_r: float = 1.0
    scale_in_add_ratio: float = 0.5

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
    model_swap_costs: bool = False
    swap_rates_path: str = "data/swap_rates.json"
    swap_rollover_hour_utc: int = 21
    swap_triple_weekday: int = 2  # Wednesday (Mon=0)

    # ── Volatility stop model (F1 Phase A: offline evidence only) ────────
    # Controls ATR-based stop-loss distance computation for offline
    # backtest comparison.  "off" = live SL is unaffected; backtest
    # comparison harness uses compare_atr_stop=True opt-in.
    # "shadow"/"on" are reserved for a FUTURE PR-B and are NOT
    # implemented here — the live _resolve_stop_loss is byte-for-byte
    # unchanged regardless of this value.
    volatility_stop_mode: str = "on"
    atr_stop_period: int = 14
    atr_stop_mult: float = 1.5
    atr_stop_ratio_min: float = 0.5
    atr_stop_ratio_max: float = 2.0
    atr_stop_max_risk_mult: float = 4.0

    # ── Spread / slippage deterioration monitoring ────────────────────────
    spread_monitor_enabled: bool = True
    # If spread widens beyond N× normal for this instrument, tighten SL
    spread_deterioration_multiplier: float = 3.0

    # ── HTF candle close reassessment ────────────────────────────────────
    htf_reassessment_enabled: bool = True
    # Check H1 candle close direction — if against trade, exit
    htf_reassess_on_h1_close: bool = True


# ---------------------------------------------------------------------------
# Application config
# ---------------------------------------------------------------------------

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
    scan_interval_seconds: int = 10
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

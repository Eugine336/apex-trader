"""
APEX TRADER — Configuration
All settings in one place. Per-instrument pip sizes, scoring thresholds,
risk parameters, and the complete instrument registry covering Forex,
commodities, indices, and Deriv synthetics.
"""

from dataclasses import dataclass, field
from enum import Enum


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
    watchlist_score: int = 50
    structure_points: int = 20
    order_block_points: int = 20
    fvg_points: int = 15
    mtf_confluence_points: int = 15
    session_points: int = 10
    news_points: int = 10
    currency_strength_points: int = 10
    ranging_score_cap: int = 55  # Tighter cap — ranging pairs need stronger confluences
    volatile_score_cap: int = 100  # FIX: was 0 — killed all volatile-regime trades


# ---------------------------------------------------------------------------
# Risk parameters
# ---------------------------------------------------------------------------

# NOTE: These are conservative defaults for the initial live validation phase.
# Once the strategy has 200+ trades and shows positive expectancy, increase
# risk_per_trade_pct to 1.0, then 2.0. Never increase before proving edge.
@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    max_daily_drawdown_pct: float = 3.0
    max_open_trades: int = 3
    max_correlated_trades: int = 1
    min_risk_reward: float = 1.5
    # How many times the instrument's typical spread we allow before rejecting.
    # 3.0 gives headroom for indices (US100 widens ~10 pts off-hours vs typical 1.5)
    # and crypto (BTCUSD spreads balloon on thin liquidity).
    # The validator uses: max_allowed = typical_spread * max_spread_multiplier
    max_spread_multiplier: float = 3.0
    ev_threshold: float = -0.1
    tp_adjust_enabled: bool = False
    pending_orders_enabled: bool = False
    pending_max_wait_minutes: int = 30


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
    except KeyError:
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
    except KeyError:
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

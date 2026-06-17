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
    volatile_score_cap: int = 100  # Cap applied to confluence score in a VOLATILE regime (100 = effectively uncapped)
    use_adaptive_scoring_weights: bool = True
    # ── M1 pattern confluence (collapse #21) ─────────────────────────────
    # ``get_best_pattern`` keeps only the single strongest M1 confirmation;
    # co-occurring confirmations (e.g. engulfing + pin bar + volume spike) are
    # discarded. When this is on, the entry engine adds a small bounded bonus
    # for EXTRA simultaneous confirmations beyond the strongest one, capped at
    # ``pattern_confluence_max_bonus``. Default OFF — behaviour unchanged.
    pattern_confluence_bonus: bool = True
    pattern_confluence_max_bonus: int = 2
    # ── Per-class scoring weights (learning layer) ───────────────────────
    # A single global weight set averages forex + synthetic + crypto into a
    # mediocre middle. When ``per_class_optimizer`` is on the ScoreOptimizer
    # keeps a separate weight profile per asset class (forex/synthetic/crypto/
    # commodity/index) plus a shared ``default`` used for cold-start, unknown
    # symbols, and as the Bayesian-shrinkage prior for thin classes. Default
    # OFF — single-profile behaviour is preserved byte-for-byte until enabled.
    per_class_optimizer: bool = True
    # Minimum trades a class needs before it gets an independent profile;
    # below this it resolves to the shared default weights.
    min_trades_per_class: int = 30
    # Bayesian shrinkage strength — how strongly a class profile is pulled
    # toward the global default (higher = more shrinkage for thin classes).
    class_shrinkage_strength: float = 0.3


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
    # PR10 Phase 0: when the panel collapses to NEUTRAL on the agreement gate,
    # log the suppressed minority cluster and emit a counterfactual shadow so
    # the opportunity cost of the collapse can be measured. Logging/shadow only
    # — it never changes the consensus verdict.
    log_suppressed_minorities: bool = True
    # ── Signal-fidelity flags (Session 4) ─────────────────────────────────
    # #11 — derive the momentum vote's confidence continuously from how far RSI
    # is past the 70/30 extreme and the MACD histogram magnitude, instead of the
    # legacy hardcoded 0.8 (both agree) / 0.4 (one source). Set False to restore
    # the constant confidences.
    momentum_continuous_confidence: bool = True
    # #25 — let stacked same-side order-block / FVG zones reinforce each other
    # (three bullish OBs read stronger than one) instead of only the single best
    # zone counting. ``zone_confluence_step`` is the diminishing weight each extra
    # stacked zone adds on top of the best. Set ``zone_confluence_bonus=False`` to
    # restore the legacy best-per-side ``max()``.
    zone_confluence_bonus: bool = True
    zone_confluence_step: float = 0.15
    # #26 — carry each module's richer secondary read (RSI level, MACD histogram,
    # zone stacking, sweep type, …) onto the Vote.evidence map instead of
    # discarding it at the (direction, confidence) collapse. Additive context for
    # the ranker / orchestrator / dashboard — never changes the consensus verdict.
    carry_vote_evidence: bool = True

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
# Opportunity ranker — open-ended trade ideas from the same module votes.
# Instead of collapsing votes to one scalar (LONG/SHORT/NEUTRAL), coherent vote
# clusters (direction × timeframe) are scored as independent opportunities with
# their own expected value.  Additive: ``enabled`` controls computing candidates
# (shadow), ``execute`` controls whether the executor selects the live direction.
# ---------------------------------------------------------------------------

_DEFAULT_SCALP_MODULES: list[str] = ["momentum", "volume", "vwap", "liquidity"]
_DEFAULT_SWING_MODULES: list[str] = [
    "structure",
    "currency_strength",
    "wyckoff",
    "order_block",
    "fvg",
]


@dataclass
class OpportunityRankerConfig:
    enabled: bool = True            # compute + attach ranked candidates
    execute: bool = True            # LIVE: the executor picks the live direction
    scalp_modules: list[str] = field(default_factory=lambda: list(_DEFAULT_SCALP_MODULES))
    swing_modules: list[str] = field(default_factory=lambda: list(_DEFAULT_SWING_MODULES))
    scalp_reward_risk: float = 1.5
    swing_reward_risk: float = 2.5
    base_win_rate: float = 0.40
    confidence_win_rate_gain: float = 0.40
    min_expected_value: float = 0.0       # R — drop opportunities below this EV
    min_cluster_confidence: float = 0.0
    min_cluster_contributors: int = 1
    max_concurrent: int = 1               # executor: max opportunities per result

    # ── Capacity-aware dispatch (collapse #13 / #15) ─────────────────────
    # The main loop historically dispatched a hardcoded ``reranked[:3]`` READY
    # setups per cycle — the 4th+ best opportunity was dropped regardless of its
    # quality or whether trade slots were free. ``dispatch_top_n`` makes that cut
    # configurable (default 3 = legacy behaviour). With ``slot_aware_dispatch``
    # on, the cut instead tracks the real free trade slots (max_open_trades minus
    # open positions), bounded by ``dispatch_max_n`` — so the ranked tail is only
    # cut by available capacity, never a magic number. Downstream gates
    # (correlation/CP4, margin, max-trades, planner, governor) are unchanged and
    # still independently approve or reject each dispatched setup.
    dispatch_top_n: int = 3
    slot_aware_dispatch: bool = True
    dispatch_max_n: int = 10

    # When the scalar ``decide`` consensus collapses a mixed panel (fast vs slow
    # modules disagreeing on horizon) to NEUTRAL, the scanner marks the setup
    # WAITING (non-tradeable) BEFORE the main-loop executor ever runs — so the
    # ranker can never act on the coherent opportunities it already scored. With
    # this on (and ``execute`` on), the scanner promotes such a NEUTRAL setup to
    # the ranker's best-EV direction at the scan stage so it can reach READY and
    # flow through the unchanged entry pipeline. False = legacy behaviour (the
    # ranker only confirms/overrides setups that were already directional).
    rescue_neutral_consensus: bool = True

    # ── HTF demotion to pure context (per selected-opportunity horizon) ──
    # When the ranker selects the live direction, the higher-timeframe (H4/D1)
    # bias downstream is scaled by the opportunity's horizon instead of holding
    # blanket authority. A SCALP idea (fast modules) should not be suppressed by
    # an opposing H4 it does not trade on; a SWING idea should still respect it.
    # These multipliers apply to BOTH the EntryEngine H4 counter-trend penalty
    # and the DecisionEngine HTF enter/skip/conviction weights. They are inert
    # (full HTF authority, scale 1.0) for any trade with no ranker horizon —
    # e.g. the scalar fallback — so behaviour is unchanged when no candidate is
    # selected. 0.0 = HTF fully demoted to context; 1.0 = full HTF authority.
    scalp_htf_penalty_scale: float = 0.0
    swing_htf_penalty_scale: float = 1.0
    mixed_htf_penalty_scale: float = 0.5

    # ── Adaptive win-rate provider (learning layer #1) ───────────────────
    # The ranker's win probability defaults to a modelled formula built on the
    # constant ``base_win_rate`` above, so the orchestrator sizes every trade on
    # a hardcoded 0.40 even though PairLearner / EVEstimator already hold real
    # per-pair / regime / session win rates. When this is on, a per-pair
    # provider supplies an OBSERVED win rate (PairLearner → EVEstimator →
    # cold-start prior) to the ranker hook so its EV — and the sizing that
    # consumes it — runs on real history. False = legacy (no provider supplied,
    # behaviour unchanged). LIVE: the ranker sizes on observed per-pair history.
    adaptive_win_rate_provider_enabled: bool = True
    # Bayesian shrinkage toward the prior so a thin sample never yields an
    # extreme rate: blended = (n*observed + prior_strength*prior)/(n+prior_strength).
    # Below ``adaptive_win_rate_min_trades`` the observed rate is blended; at or
    # above it the rate is used directly (still clamped). The prior matches the
    # ranker constant so cold start is unchanged.
    adaptive_win_rate_prior: float = 0.40
    adaptive_win_rate_prior_strength: int = 10
    adaptive_win_rate_min_trades: int = 10
    # Clamp the final win probability so a degenerate sample can never drive an
    # extreme position size.
    adaptive_win_rate_clamp_low: float = 0.15
    adaptive_win_rate_clamp_high: float = 0.85

    def __post_init__(self) -> None:
        if not isinstance(self.min_cluster_contributors, int) or self.min_cluster_contributors < 1:
            raise ValueError(
                "OpportunityRankerConfig.min_cluster_contributors must be an int >= 1, "
                f"got {self.min_cluster_contributors!r}"
            )
        if not isinstance(self.max_concurrent, int) or self.max_concurrent < 1:
            raise ValueError(
                "OpportunityRankerConfig.max_concurrent must be an int >= 1, "
                f"got {self.max_concurrent!r}"
            )
        for label, val in [
            ("dispatch_top_n", self.dispatch_top_n),
            ("dispatch_max_n", self.dispatch_max_n),
        ]:
            if not isinstance(val, int) or val < 1:
                raise ValueError(
                    f"OpportunityRankerConfig.{label} must be an int >= 1, got {val!r}"
                )
        for label, val in [
            ("scalp_reward_risk", self.scalp_reward_risk),
            ("swing_reward_risk", self.swing_reward_risk),
        ]:
            if not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
                raise ValueError(
                    f"OpportunityRankerConfig.{label} must be finite > 0, got {val!r}"
                )
        for label, val in [
            ("base_win_rate", self.base_win_rate),
            ("confidence_win_rate_gain", self.confidence_win_rate_gain),
        ]:
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                raise ValueError(
                    f"OpportunityRankerConfig.{label} must be in [0, 1], got {val!r}"
                )
        for label, val in [
            ("scalp_htf_penalty_scale", self.scalp_htf_penalty_scale),
            ("swing_htf_penalty_scale", self.swing_htf_penalty_scale),
            ("mixed_htf_penalty_scale", self.mixed_htf_penalty_scale),
        ]:
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                raise ValueError(
                    f"OpportunityRankerConfig.{label} must be in [0, 1], got {val!r}"
                )
        for label, val in [
            ("adaptive_win_rate_prior", self.adaptive_win_rate_prior),
            ("adaptive_win_rate_clamp_low", self.adaptive_win_rate_clamp_low),
            ("adaptive_win_rate_clamp_high", self.adaptive_win_rate_clamp_high),
        ]:
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                raise ValueError(
                    f"OpportunityRankerConfig.{label} must be in [0, 1], got {val!r}"
                )
        if self.adaptive_win_rate_clamp_low > self.adaptive_win_rate_clamp_high:
            raise ValueError(
                "OpportunityRankerConfig.adaptive_win_rate_clamp_low must be <= "
                f"adaptive_win_rate_clamp_high, got "
                f"{self.adaptive_win_rate_clamp_low} > {self.adaptive_win_rate_clamp_high}"
            )
        if not isinstance(self.adaptive_win_rate_prior_strength, int) or self.adaptive_win_rate_prior_strength < 0:
            raise ValueError(
                "OpportunityRankerConfig.adaptive_win_rate_prior_strength must be an int >= 0, "
                f"got {self.adaptive_win_rate_prior_strength!r}"
            )
        if not isinstance(self.adaptive_win_rate_min_trades, int) or self.adaptive_win_rate_min_trades < 1:
            raise ValueError(
                "OpportunityRankerConfig.adaptive_win_rate_min_trades must be an int >= 1, "
                f"got {self.adaptive_win_rate_min_trades!r}"
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
    # Confluence-score co-gate for READY (P5). The 0-123 confluence score must
    # clear this floor *in addition to* OQ/EQ before a setup qualifies as READY.
    # 85 matches the lowest sizing tier in RiskEngine._scale_risk_by_score
    # (below 85 the system already halves size — too weak to trade at all).
    # Set <= 0 to disable the score co-gate (OQ/EQ alone gate READY, legacy).
    ready_min_score: int = 85
    # Entry-time re-validation floors (P1). When a READY setup is actually
    # executed (seconds-to-minutes after the scan), OQ/EQ are recomputed from
    # fresh candles; the entry is rejected if either has decayed below these
    # floors. Kept slightly below the READY thresholds to avoid flickering
    # rejections on borderline setups.
    revalidate_opportunity_quality_min: float = 5.0
    revalidate_entry_quality_min: float = 4.0
    oq_weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_OQ_WEIGHTS))
    eq_weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_EQ_WEIGHTS))

    def __post_init__(self) -> None:
        for name, val in [
            ("opportunity_quality_min", self.opportunity_quality_min),
            ("entry_quality_min", self.entry_quality_min),
            ("revalidate_opportunity_quality_min", self.revalidate_opportunity_quality_min),
            ("revalidate_entry_quality_min", self.revalidate_entry_quality_min),
        ]:
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                raise ValueError(
                    f"LayeredDecisionConfig.{name} must be finite, got {val!r}"
                )
            if val < 0 or val > 10:
                raise ValueError(
                    f"LayeredDecisionConfig.{name} must be in [0, 10], got {val!r}"
                )

        if not isinstance(self.ready_min_score, (int, float)) or not math.isfinite(self.ready_min_score):
            raise ValueError(
                f"LayeredDecisionConfig.ready_min_score must be finite, got {self.ready_min_score!r}"
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
    # #33 — graded defensive gates. The EV gate, losing-pattern gate and the ML
    # should_trade veto each hard `return False` (legacy "veto"). When set to
    # "penalty" the setup instead flows through carrying a bounded size haircut
    # (the orchestrator/risk stack folds it into the final lots) — rich evidence
    # is no longer destroyed by a single first-breach kill. Default "veto" keeps
    # the legacy hard rejection. Fail-safe error paths always stay hard skips.
    ev_gate_mode: str = "veto"               # "veto" | "penalty"
    ev_gate_below_size_mult: float = 0.5
    losing_pattern_mode: str = "veto"        # "veto" | "penalty"
    losing_pattern_size_mult: float = 0.5
    ml_should_trade_mode: str = "veto"       # "veto" | "penalty"
    ml_should_trade_size_mult: float = 0.5
    tp_adjust_enabled: bool = True
    pending_orders_enabled: bool = True
    pending_max_wait_minutes: int = 30
    max_cluster_same_direction: int = 2
    allow_intentional_hedge: bool = True
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
    # risk-engine-sized lot upward (e.g. 0.01 → 0.5).  LIVE: an inflated
    # min-lot order is rejected rather than silently proceeding oversized.
    reject_on_minlot_inflation: bool = True

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

    # ── Drawdown guard ──────────────────────────────────────────────────
    # Trailing window (calendar days) over which drawdown-from-peak is
    # measured for the consumers that gate sizing (planner size-reduction
    # and RiskEngine haircut). A lifetime measure never resets, so an
    # account that bled deeply once stays shrunk indefinitely; the rolling
    # window lets sizing recover as recent equity does. <= 0 disables the
    # window (falls back to the lifetime high-water mark).
    drawdown_rolling_window_days: int = 30

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

    # #32 — tf-alignment-aware structure exit. The M5 structure-exit fires a
    # full close on the first counter-direction CHoCH/BOS and ignores the
    # strategic tf_alignment it already records. When enabled, a fresh strategic
    # tf_alignment that still strongly supports the trade direction (|·| ≥
    # structure_exit_tf_alignment_defer, signed toward the trade) defers that
    # mechanical exit — the HTF trend treats the M5 break as noise. LIVE: a
    # strongly-supportive HTF alignment defers the binary structure exit.
    structure_exit_tf_alignment_enabled: bool = True
    structure_exit_tf_alignment_defer: float = 0.5
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

    # ── P8: portfolio-heat-aware trailing (mechanical manager) ────────────
    # The portfolio-heat state machine reacts to heat at the BOOK level
    # (DEFENSIVE/REDUCING/EMERGENCY trims/closes). The mechanical TradeManager,
    # however, trails every runner at the same width regardless of how stressed
    # the book is. When the book is hot, a runner in profit should lock gains
    # faster: multiply the structure-trail buffer by a heat-dependent factor so
    # the stop sits CLOSER to structure (tighter). A smaller factor → tighter
    # trail. This only ever moves SL in the profit direction (the trail itself
    # enforces never-worsen-SL), only applies post-breakeven, and falls back to
    # the normal width (factor 1.0) on any error. Set enabled=False to disable.
    heat_trail_tighten_enabled: bool = True
    heat_trail_factor_defensive: float = 0.7   # DEFENSIVE → 0.7× trail buffer
    heat_trail_factor_reducing: float = 0.5    # REDUCING  → 0.5× trail buffer
    heat_trail_factor_emergency: float = 0.5   # EMERGENCY → 0.5× trail buffer

    # ── P9: periodic management-status log cadence (cycles) ───────────────
    # Every N supervised cycles, emit a structured INFO summary of management
    # mode (positions, heat state, strategic status, verdict distribution) for
    # operational visibility. Set to 0 to disable the periodic summary.
    management_status_log_interval_cycles: int = 100

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

        # P8: heat-trail factors must be in (0, 1] — a factor > 1 would WIDEN the
        # trail when the book is stressed (the opposite of the intent) and a
        # factor <= 0 would collapse the buffer onto structure.
        def _check_trail_factor(name: str, val: float) -> None:
            if not isinstance(val, (int, float)) or not math.isfinite(val) or not (0.0 < val <= 1.0):
                raise ValueError(
                    f"RiskConfig.{name} must be a finite number in (0, 1], got {val!r}"
                )

        _check_trail_factor("heat_trail_factor_defensive", self.heat_trail_factor_defensive)
        _check_trail_factor("heat_trail_factor_reducing", self.heat_trail_factor_reducing)
        _check_trail_factor("heat_trail_factor_emergency", self.heat_trail_factor_emergency)
        if (
            not isinstance(self.management_status_log_interval_cycles, int)
            or self.management_status_log_interval_cycles < 0
        ):
            raise ValueError(
                "RiskConfig.management_status_log_interval_cycles must be an int >= 0, "
                f"got {self.management_status_log_interval_cycles!r}"
            )

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
    # #17 — weighted reversal evidence. Legacy counts the M5-sweep / M1-BOS /
    # momentum signals and gates on an integer (≥ reversal_required_evidence),
    # so a +0.21 momentum reads identical to +0.95 and two strong signals lose
    # to three weak ones. When enabled, each signal contributes a continuous
    # strength and the gate compares the summed strength to
    # reversal_required_strength (default 2.0 ≈ two full signals). LIVE: each
    # signal contributes a continuous strength rather than an integer count.
    reversal_weighted_evidence: bool = True
    reversal_required_strength: float = 2.0
    reversal_momentum_full: float = 0.6      # momentum reaching this counts as full strength
    # ── HTF = bounded context (Scenario A) ────────────────────────────────
    # When the full HTF stack (D1+H4+H1) supports the trade direction, give a
    # bounded size BONUS on top of the conviction model — HTF helps when it
    # agrees, hurts (reversal haircut) when it opposes, but never dictates.
    htf_aligned_size_bonus: float = 0.15     # +15% size on a fully-aligned stack
    htf_aligned_threshold: float = 0.5       # min tf_alignment to count as "aligned"
    # ── Conviction → size mapping (#18, smooth curves) ────────────────────
    # Conviction 0→min, 1→max, mapped linearly (continuous, no tier cliffs, so
    # 0.879 and 0.851 size differently). Defaults reproduce the legacy 0.5–1.5×
    # mapping; widen (e.g. 0.25–2.0) to let strong/weak conviction express more.
    conviction_size_min: float = 0.5
    conviction_size_max: float = 1.5
    # MARKET vs PENDING preference cutoff on the (now smooth) market score.
    market_mode_threshold: float = 0.40
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
    # ── Live OQ/EQ decay management (P3) ──────────────────────────────────
    # OQ/EQ gated the trade READY at entry; they are recomputed on fresh
    # candles during management (decision.context.live_oq/live_eq). When the
    # market conditions (OQ) or entry geometry (EQ) that justified the trade
    # decay, add bounded CLOSE/TIGHTEN pressure — "the reason this was tradeable
    # is gone". Additive weighted terms, never a hard override; inert when the
    # live scores are unavailable (None).
    oq_eq_decay_enabled: bool = True
    oq_floor: float = 5.0                    # live OQ below this → CLOSE/TIGHTEN pressure
    eq_floor: float = 5.0                    # live EQ below this → TIGHTEN pressure
    oq_decay_significant: float = 2.0        # OQ drop (even above floor) → TIGHTEN pressure
    # ── Fast-cluster opposition decay (PR10) ──────────────────────────────
    # Data showed the management engine holds losing trades while the fast-
    # evidence cluster (momentum + M1 alignment) has flipped against the
    # position, anchored by "HTF aligned" as the hold reason. When the fast
    # cluster has opposed for ``fast_opposition_min_streak`` consecutive
    # management cycles AND the trade is NOT meaningfully in profit (profit_r <
    # fast_opposition_profit_threshold), add bounded, progressively-ramping
    # CLOSE pressure (weight × min(streak/max_streak, 1)). Additive only — it
    # never overrides a stronger verdict and never touches the stop. Winners are
    # unaffected. Set enabled=False to disable.
    fast_opposition_decay_enabled: bool = True
    fast_opposition_min_streak: int = 3      # cycles of opposition before pressure starts
    fast_opposition_max_streak: int = 8      # streak at which the pressure ramp caps
    fast_opposition_decay_weight: float = 0.15  # max CLOSE pressure at full ramp
    fast_opposition_profit_threshold: float = 0.3  # only applies below this R


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
# Performance — hot-path latency controls (Phase 5).
# Caches and parallelism that cut the scan-cycle floor (dominated by throttled
# broker candle fetches) without changing any decision logic. TTLs are kept
# under each timeframe's bar period so a fresh bar is always available at close;
# better to re-fetch than to ever trade on stale intrabar data.
# ---------------------------------------------------------------------------


@dataclass
class PerformanceConfig:
    # ── Intraday candle cache (Fix #1) ──
    # Transparent TTL cache around per-symbol/timeframe broker fetches. Kills
    # repeated full re-fetches of M5/M15/H1/H4 every cycle. Keyed by
    # (symbol, timeframe, count); a hit returns the cached DataFrame and skips
    # the broker entirely. TTLs sit just under the bar period.
    candle_cache_enabled: bool = True
    candle_cache_ttl: dict[str, float] = field(
        default_factory=lambda: {
            "M1": 1.0,
            "M5": 5.0,
            "M15": 15.0,
            "M30": 25.0,
            "H1": 55.0,
            "H4": 240.0,
            "D1": 3600.0,
        }
    )
    candle_cache_default_ttl: float = 5.0  # unknown timeframes — conservative
    # M1 TTL knob — dropped to 1s so a freshly closed M1 bar (and the engulfing
    # it completes) is detected within ~1s of close instead of up to 3s. This is
    # the authoritative M1 setting and overrides candle_cache_ttl["M1"].
    m1_cache_ttl: float = 1.0

    # ── Scan cadence (M1 scalping) ──
    # Active/overlap sessions and any cycle with open positions scan every 5s so
    # a closed M1 engulfing is acted on within ~5s of bar close instead of the
    # old 10–15s. Quiet/dead/news cadences are unchanged (see ScanScheduler).
    scan_interval_active: int = 5
    scan_interval_with_positions: int = 5

    # ── Parallel pair scan (Fix #3) ──
    # The 9 analysis modules are pure pandas/numpy (no broker I/O), so pairs are
    # scanned concurrently. 0 = auto = min(parallel_scan_max_workers, n_pairs).
    parallel_scan_enabled: bool = True
    parallel_scan_max_workers: int = 10

    # ── Account info cache (Fix #4) ──
    # Margin level doesn't change between candidates in one cycle — cache it
    # for the duration of a scan cycle (keyed by cycle id).
    account_info_cache_enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.parallel_scan_max_workers, int) or self.parallel_scan_max_workers < 1:
            raise ValueError(
                "PerformanceConfig.parallel_scan_max_workers must be an int >= 1, "
                f"got {self.parallel_scan_max_workers!r}"
            )
        if not isinstance(self.candle_cache_default_ttl, (int, float)) or \
                not math.isfinite(self.candle_cache_default_ttl) or self.candle_cache_default_ttl <= 0:
            raise ValueError(
                "PerformanceConfig.candle_cache_default_ttl must be finite > 0, "
                f"got {self.candle_cache_default_ttl!r}"
            )
        for tf, ttl in self.candle_cache_ttl.items():
            if not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
                raise ValueError(
                    f"PerformanceConfig.candle_cache_ttl[{tf!r}] must be finite > 0, got {ttl!r}"
                )
        if not isinstance(self.m1_cache_ttl, (int, float)) or \
                not math.isfinite(self.m1_cache_ttl) or self.m1_cache_ttl <= 0:
            raise ValueError(
                f"PerformanceConfig.m1_cache_ttl must be finite > 0, got {self.m1_cache_ttl!r}"
            )
        # m1_cache_ttl is the authoritative M1 knob — keep the TTL dict in sync.
        self.candle_cache_ttl["M1"] = float(self.m1_cache_ttl)
        for name in ("scan_interval_active", "scan_interval_with_positions"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(
                    f"PerformanceConfig.{name} must be an int >= 1, got {value!r}"
                )


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
class DecisionTraceConfig:
    """Settings for the pipeline Decision Trace (component awareness layer).

    When ``enabled`` the trading loop threads a DecisionTrace through every
    entry-pipeline stage; each stage stamps a justified verdict and downstream
    stages may challenge upstream ones.  Completed traces are emitted as
    DECISION_TRACE events for the dashboard's pipeline panels.  Purely additive —
    it records decisions, it never changes them.
    """

    enabled: bool = True


@dataclass
class OrchestratorConfig:
    """Settings for the Trade Orchestrator (the round table / graded sizer).

    The orchestrator collects every stage's evidence (ranker EV/coherence,
    situation HTF alignment, decision-engine margin/conviction, planner advisor
    agreement, scan score) and folds each dimension into a *bounded size
    multiplier* in ``[size_floor, 1.0]`` — weak dimensions dim the size, they
    never extinguish the trade.  The only hard vetoes are physics (negative
    margin, market closed, duplicate, below broker min lot), which the live risk
    gates already enforce — so the orchestrator can only SIZE DOWN a trade the
    pipeline already approved, never place one it would have refused.

    ``enabled`` — collect + record the proposal/verdict on every entry attempt
    (drives the dashboard + outcome feedback). ``apply_sizing`` — let the graded
    multiplier actually scale the live position. Both LIVE by default per the
    "live, not shadow, but fully logged" mandate; flip ``apply_sizing`` off to
    record without touching live lots.
    """

    enabled: bool = True
    apply_sizing: bool = True
    # Lower bound on the overall multiplier — a graded "no" is still a small
    # trade, never zero (physics vetoes aside). 1.0 would make it inert.
    size_floor: float = 0.5
    # Lower bound on each individual dimension's contribution.
    dimension_floor: float = 0.6
    # Per-dimension "full credit" reference points.
    ranker_ev_full: float = 1.5      # R units at which ranker EV gives full size
    de_margin_full: float = 0.5      # enter-skip margin at which DE gives full size
    scan_score_full: float = 100.0   # confluence score giving full size
    # ── #6 — Decision-engine enter/skip gate softening ───────────────────
    # The DE's enter/skip binary (margin <= 0 → SKIP) was the last CRITICAL
    # collapse that killed setups before the round table ever saw them. When
    # ``soften_de_gate`` is on, a *mildly* negative margin no longer kills the
    # setup — it flows through as ENTER carrying a bounded quality multiplier and
    # the orchestrator decides how big. Only honoured when the orchestrator is
    # enabled (the caller gates it). A margin at/below ``de_safety_margin`` is
    # genuinely hopeless and still hard-SKIPs; ``de_gate_quality_floor`` is the
    # smallest quality multiplier a softened setup can carry.
    soften_de_gate: bool = True
    de_safety_margin: float = -1.0
    de_gate_quality_floor: float = 0.15
    # Per-dimension "full credit" reference for the softened-DE quality gradient
    # the orchestrator folds in (the margin-derived multiplier is already bounded
    # in [de_gate_quality_floor, 1.0] by the decision engine).
    # How much an opposing HTF dims a SCALP (vs a SWING which feels it fully).
    scalp_htf_opposition_scale: float = 0.3
    # NOTE: per-cycle dispatch capacity is owned by OpportunityRankerConfig
    # (``dispatch_top_n`` / ``slot_aware_dispatch`` / ``max_concurrent``), which
    # is the single authority the main loop consumes. A separate orchestrator
    # ``max_concurrent_trades`` was never read — removed to avoid a dead knob.

    # ── Upstream gate softening (Phase 9: kill-switch → bounded dimmer) ────
    # When the orchestrator is the live sizer it grades every surviving setup
    # into a bounded size. The upstream QUALITY gates (scanner READY floors,
    # the entry-time OQ/EQ re-validation, the planner conviction floor, the
    # entry-score floor) therefore no longer need to *kill* a marginal setup —
    # they hand it through carrying a quality multiplier the orchestrator folds
    # into size, so a near-miss trades SMALL instead of dying. Each gate is
    # independently switchable; all default ON when the orchestrator runs.
    # Hard SAFETY floors below remain absolute — a truly hopeless setup still
    # dies, and the broker/governor/correlation PHYSICS gates are untouched.
    soften_scanner_gates: bool = True
    soften_planner_gates: bool = True
    soften_entry_gates: bool = True
    # ── #24 — accumulated risk scoring (first-breach kill → graded dimmer) ──
    # When on, the risk governor's entry review and the portfolio governor's
    # analytical concentration limits stop hard-vetoing on the FIRST breach:
    # every dimension is measured and the analytical ones (heat, spread, R:R,
    # currency / sector / correlated exposure, trade-slot headroom) fold into a
    # bounded risk multiplier the round table sizes by. Physics stays hard
    # (position limits, daily-loss halt, broker margin). Only honoured when the
    # orchestrator is enabled (the caller gates it).
    accumulate_risk: bool = True
    # Lower bound on the accumulated risk multiplier — a graded "near every
    # limit" is still a small trade, never zero.
    risk_multiplier_floor: float = 0.15
    # Lower bound on any single softened gate's quality multiplier — a graded
    # "barely passed" is still a tiny trade, never zero.
    gate_quality_floor: float = 0.15
    # Hard safety floors: a setup BELOW these is still killed (never softened).
    scanner_safety_oq: float = 2.0
    scanner_safety_eq: float = 2.0
    scanner_safety_score: float = 50.0
    entry_safety_score: float = 40.0

    # ── Live position management (round table for OPEN trades) ────────────
    # Every cycle the orchestrator re-evaluates each open position into a
    # continuous health score (product of dimension healths) and maps it to a
    # bounded management action — replacing the old argmax management collapse.
    # It is a one-way de-risk (hold/tighten/trim/exit); SCALE_UP is opt-in.
    manage_open_positions: bool = True
    allow_scale_up: bool = True
    # Per-dimension lower bound for the HEALTH product. Unlike entry sizing
    # (dimension_floor), an open position is protecting capital already at risk,
    # so a destroyed dimension is allowed to drive an exit → floor defaults to 0.
    health_dimension_floor: float = 0.0
    # Health → action thresholds (>= hold: HOLD; >= tighten: TIGHTEN_SL;
    # >= scale_down: SCALE_DOWN; >= exit_partial: EXIT_PARTIAL; else EXIT_FULL).
    health_thresholds: dict[str, float] = field(
        default_factory=lambda: {
            "hold": 0.8, "tighten": 0.6, "scale_down": 0.4, "exit_partial": 0.2,
        }
    )
    # Reference points for the health dimensions.
    risk_heat_full_pct: float = 100.0      # portfolio heat giving full risk dim
    risk_drawdown_full_r: float = 2.0      # loss (R) fully draining risk dim
    profit_loss_full_r: float = 1.0        # loss (R) fully draining profit dim
    time_decay_span_mult: float = 2.0      # decay reaches floor at N×expected hold
    situation_shift_full: float = 1.0      # adverse shift fully draining the dim
    thesis_change_flag: float = 0.6        # dim below this is flagged as a change
    # Expected holding period (minutes) per horizon — drives the time-decay dim.
    expected_hold_minutes_scalp: float = 30.0
    expected_hold_minutes_swing: float = 240.0
    # Trim fractions (portion to CLOSE) for the partial actions.
    scale_down_close_pct: float = 0.33
    exit_partial_close_pct: float = 0.6
    # SCALE_UP gating (only honoured when allow_scale_up=True).
    scale_up_min_health: float = 0.85
    scale_up_min_delta: float = 0.1
    # Pacing: don't manage immediately after entry, nor every single cycle.
    min_cycles_before_management: int = 3
    management_cooldown_cycles: int = 2

    def __post_init__(self) -> None:
        for label, val in [
            ("size_floor", self.size_floor),
            ("dimension_floor", self.dimension_floor),
            ("scalp_htf_opposition_scale", self.scalp_htf_opposition_scale),
            ("health_dimension_floor", self.health_dimension_floor),
            ("scale_up_min_health", self.scale_up_min_health),
            ("scale_up_min_delta", self.scale_up_min_delta),
            ("scale_down_close_pct", self.scale_down_close_pct),
            ("exit_partial_close_pct", self.exit_partial_close_pct),
            ("de_gate_quality_floor", self.de_gate_quality_floor),
            ("risk_multiplier_floor", self.risk_multiplier_floor),
        ]:
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                raise ValueError(
                    f"OrchestratorConfig.{label} must be in [0, 1], got {val!r}"
                )
        if not isinstance(self.de_safety_margin, (int, float)) or not math.isfinite(self.de_safety_margin) or self.de_safety_margin > 0:
            raise ValueError(
                "OrchestratorConfig.de_safety_margin must be a finite float <= 0, "
                f"got {self.de_safety_margin!r}"
            )
        for label, val in [
            ("ranker_ev_full", self.ranker_ev_full),
            ("de_margin_full", self.de_margin_full),
            ("scan_score_full", self.scan_score_full),
            ("risk_heat_full_pct", self.risk_heat_full_pct),
            ("risk_drawdown_full_r", self.risk_drawdown_full_r),
            ("profit_loss_full_r", self.profit_loss_full_r),
            ("time_decay_span_mult", self.time_decay_span_mult),
            ("situation_shift_full", self.situation_shift_full),
            ("expected_hold_minutes_scalp", self.expected_hold_minutes_scalp),
            ("expected_hold_minutes_swing", self.expected_hold_minutes_swing),
        ]:
            if not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
                raise ValueError(
                    f"OrchestratorConfig.{label} must be finite > 0, got {val!r}"
                )
        for label, val in [
            ("min_cycles_before_management", self.min_cycles_before_management),
            ("management_cooldown_cycles", self.management_cooldown_cycles),
        ]:
            if not isinstance(val, int) or val < 0:
                raise ValueError(
                    f"OrchestratorConfig.{label} must be an int >= 0, got {val!r}"
                )
        if not isinstance(self.health_thresholds, dict):
            raise ValueError("OrchestratorConfig.health_thresholds must be a dict")
        for label, val in [
            ("scanner_safety_oq", self.scanner_safety_oq),
            ("scanner_safety_eq", self.scanner_safety_eq),
            ("scanner_safety_score", self.scanner_safety_score),
            ("entry_safety_score", self.entry_safety_score),
        ]:
            if not isinstance(val, (int, float)) or not math.isfinite(val) or val < 0:
                raise ValueError(
                    f"OrchestratorConfig.{label} must be finite >= 0, got {val!r}"
                )


@dataclass
class OutcomeFeedbackConfig:
    """Settings for the post-trade Outcome Feedback loop.

    On entry it records which modules/opportunity drove the trade (attribution);
    on close it links the realised R back to that attribution so per-module,
    per-horizon accuracy can be measured over time and surfaced on the dashboard.
    Purely observational — it never changes a live decision.
    """

    enabled: bool = True
    journal_path: str = "data/outcome_feedback.jsonl"
    # Rolling window (most recent N completed trades) for accuracy aggregation.
    accuracy_lookback: int = 300


@dataclass
class SignalLedgerConfig:
    """Settings for the universal Signal Ledger + Emitter Feedback layer.

    Records EVERY directional read each module emits — before any gate runs —
    and grades it on whether price actually moved the predicted way, regardless
    of whether a trade was taken. Removes the selection bias in the learning
    layer (only taken trades were ever graded) and lets each emitter ask how it
    is doing and whether a gate is over-filtering its correct signals.

    Purely observational — nothing here changes a live decision. The recorder
    and grader are LIVE by default so the learning layer accumulates unbiased
    per-emitter accuracy from the start.
    """

    # Master switch: record signals at emission time.
    signal_ledger_enabled: bool = True
    # Run the background grading cycle (price sampling + finalisation).
    signal_grading_enabled: bool = True
    # Elapsed time before a signal is finalised (direction_correct decided).
    signal_grading_delay_minutes: int = 30
    # Minutes at which intermediate price observations are stamped.
    signal_grading_check_intervals: list[int] = field(
        default_factory=lambda: [5, 15, 30, 60]
    )
    # Minimum signed move (%) in the predicted direction to count as correct.
    signal_min_move_pct: float = 0.1
    # Enable the read-side EmitterFeedback service.
    emitter_feedback_enabled: bool = True
    # SQLite path (under data/, gitignored).
    signal_ledger_db_path: str = "data/signal_ledger.db"
    # Rolling window of most-recent graded signals for accuracy aggregation.
    accuracy_lookback: int = 100

    def __post_init__(self) -> None:
        if int(self.signal_grading_delay_minutes) < 0:
            raise ValueError(
                "SignalLedgerConfig.signal_grading_delay_minutes must be >= 0, "
                f"got {self.signal_grading_delay_minutes!r}"
            )
        if not isinstance(self.signal_grading_check_intervals, list) or not all(
            isinstance(x, (int, float)) and x >= 0
            for x in self.signal_grading_check_intervals
        ):
            raise ValueError(
                "SignalLedgerConfig.signal_grading_check_intervals must be a list "
                f"of non-negative numbers, got {self.signal_grading_check_intervals!r}"
            )
        if not (0.0 <= float(self.signal_min_move_pct) <= 100.0):
            raise ValueError(
                "SignalLedgerConfig.signal_min_move_pct must be in [0, 100], "
                f"got {self.signal_min_move_pct!r}"
            )
        if int(self.accuracy_lookback) <= 0:
            raise ValueError(
                "SignalLedgerConfig.accuracy_lookback must be > 0, "
                f"got {self.accuracy_lookback!r}"
            )


@dataclass
class VoteCalibratorConfig:
    """Settings for the Vote Calibrator (learning layer #6).

    The consensus math weights each module's vote by a STATIC
    ``ConsensusConfig.weights`` entry — a module that is 80%% accurate carries
    the same weight as one that is 40%% accurate. The SignalLedger already
    grades every signal (traded or blocked); this calibrator reads each
    module's track record (via the read-only EmitterFeedback service) and turns
    it into a weight multiplier centred on 1.0 — accurate modules vote louder,
    noisy ones softer.

    Safety: the multiplier is centred on 1.0 (an average-accuracy panel changes
    nothing), thin samples are shrunk toward the mean, and every multiplier is
    clamped to [floor, ceiling] so no module is silenced or allowed to dominate.
    Defaults OFF so it stays dormant until enabled.
    """

    # Master switch — replace static consensus weights with calibrated weights.
    vote_calibration_enabled: bool = True
    # How accuracy maps to a multiplier: "softmax" (exp of accuracy / temp),
    # "proportional" (accuracy / mean), or "log_odds" (exp of centred logit).
    vote_weight_method: str = "softmax"
    # Softmax / log-odds sharpness — lower = more aggressive differentiation.
    vote_weight_temperature: float = 1.0
    # Multiplier band: no module's weight multiplier drops below the floor or
    # exceeds the ceiling (base weights default to 1.0, so these bound the
    # effective weight too).
    vote_weight_floor: float = 0.1
    vote_weight_ceiling: float = 3.0
    # Minimum graded signals before a module is calibrated (else stays at 1.0).
    vote_calibration_min_signals: int = 20
    # Bayesian shrinkage toward the panel mean (0 = none, higher = pull thin
    # samples harder). Scaled by min_signals into pseudo-observations.
    vote_calibration_shrinkage: float = 0.5
    # Rolling window of most-recent graded signals used to read accuracy.
    vote_calibration_lookback: int = 100
    # Minimum seconds between recalibration passes (the TunerAgent drives it).
    vote_calibration_min_interval_seconds: float = 3600.0

    # ── Counterfactual blend (L4 integration) ────────────────────────────
    # Blend each module's MARGINAL contribution (marginal R per attributed
    # trade, read from the read-only CounterfactualEngine cache) into the weight
    # signal — the continuous complement to the Module Governor's discrete
    # shadow decision. A module that is accurate yet harmful by marginal R is
    # damped below 1.0. When off, or when no engine is wired / data is thin, the
    # calibrator uses graded accuracy alone (unchanged behaviour).
    use_counterfactual_weight: bool = True
    # Blend weight: 0 = accuracy only, 1 = marginal-R only. The accuracy and
    # marginal-R multipliers are combined as a weighted geometric mean and the
    # result is re-centred on 1.0 and re-clamped to [floor, ceiling].
    counterfactual_weight_blend: float = 0.3
    # Minimum attributed trades before a module's marginal-R signal is trusted
    # (else that module keeps its accuracy-only multiplier).
    counterfactual_weight_min_trades: int = 100

    def __post_init__(self) -> None:
        if self.vote_weight_method not in ("softmax", "proportional", "log_odds"):
            raise ValueError(
                "VoteCalibratorConfig.vote_weight_method must be one of "
                f"'softmax', 'proportional', 'log_odds', got {self.vote_weight_method!r}"
            )
        if not (float(self.vote_weight_temperature) > 0):
            raise ValueError(
                "VoteCalibratorConfig.vote_weight_temperature must be > 0, "
                f"got {self.vote_weight_temperature!r}"
            )
        if float(self.vote_weight_floor) < 0:
            raise ValueError(
                "VoteCalibratorConfig.vote_weight_floor must be >= 0, "
                f"got {self.vote_weight_floor!r}"
            )
        if float(self.vote_weight_ceiling) < float(self.vote_weight_floor):
            raise ValueError(
                "VoteCalibratorConfig.vote_weight_ceiling must be >= floor, "
                f"got ceiling={self.vote_weight_ceiling!r} floor={self.vote_weight_floor!r}"
            )
        if int(self.vote_calibration_min_signals) < 1:
            raise ValueError(
                "VoteCalibratorConfig.vote_calibration_min_signals must be >= 1, "
                f"got {self.vote_calibration_min_signals!r}"
            )
        if float(self.vote_calibration_shrinkage) < 0:
            raise ValueError(
                "VoteCalibratorConfig.vote_calibration_shrinkage must be >= 0, "
                f"got {self.vote_calibration_shrinkage!r}"
            )
        if int(self.vote_calibration_lookback) <= 0:
            raise ValueError(
                "VoteCalibratorConfig.vote_calibration_lookback must be > 0, "
                f"got {self.vote_calibration_lookback!r}"
            )
        if float(self.vote_calibration_min_interval_seconds) < 0:
            raise ValueError(
                "VoteCalibratorConfig.vote_calibration_min_interval_seconds must be >= 0, "
                f"got {self.vote_calibration_min_interval_seconds!r}"
            )
        if not (0.0 <= float(self.counterfactual_weight_blend) <= 1.0):
            raise ValueError(
                "VoteCalibratorConfig.counterfactual_weight_blend must be in [0, 1], "
                f"got {self.counterfactual_weight_blend!r}"
            )
        if int(self.counterfactual_weight_min_trades) < 1:
            raise ValueError(
                "VoteCalibratorConfig.counterfactual_weight_min_trades must be >= 1, "
                f"got {self.counterfactual_weight_min_trades!r}"
            )


@dataclass
class ModuleGovernorConfig:
    """Settings for the Module Governor (L3 — shadow mode + auto-reactivation).

    A voting module used to be either fully ON (influencing every decision) or,
    once disabled, fully OFF. This governor adds a SHADOW middle ground: a
    module whose graded accuracy drops keeps running and keeps being measured,
    but its vote weight is forced to 0.0 so it cannot influence a live decision.
    If its accuracy recovers it returns to ACTIVE; if it stays poor it is fully
    DISABLED. All transitions are driven by accuracy READ FROM the read-only
    EmitterFeedback service — the governor never grades signals itself.

    Defaults OFF so it stays dormant until enabled; when off, no module is ever
    shadowed and the consensus path is byte-for-byte unchanged.
    """

    # Master switch. When False, no module is shadowed/disabled (legacy path).
    module_governor_enabled: bool = True
    # ACTIVE → SHADOW: demote when trailing accuracy over ``shadow_lookback``
    # graded signals drops below this threshold.
    shadow_threshold: float = 0.35
    shadow_lookback: int = 50
    # SHADOW → ACTIVE: reactivate when shadow-period accuracy recovers above
    # this threshold over at least ``reactivation_min_signals`` shadow signals.
    reactivation_threshold: float = 0.50
    reactivation_min_signals: int = 30
    # SHADOW → DISABLED: fully disable when shadow-period accuracy stays below
    # this threshold over at least ``disable_min_signals`` shadow signals.
    disable_threshold: float = 0.25
    disable_min_signals: int = 50
    # DISABLED → SHADOW: if > 0, a disabled module auto-re-enters SHADOW after
    # this many days for another supervised chance. 0 = manual reactivation only.
    auto_retry_days: int = 0
    # Rolling window of most-recent graded signals read from EmitterFeedback to
    # evaluate accuracy / sample size (kept generous so shadow-period sample
    # deltas remain meaningful for active modules).
    feedback_lookback: int = 500
    # SQLite state + transition audit DB (under data/, gitignored).
    db_path: str = "data/module_governor.db"

    # ── Counterfactual signal (L4 integration) ───────────────────────────
    # The governor can consider each module's MARGINAL contribution (marginal R
    # per attributed trade, read from the read-only CounterfactualEngine cache)
    # IN ADDITION TO its graded accuracy. A module can be accurate yet harmful
    # by marginal R, so either signal alone can SHADOW it (OR'd); reactivation
    # from SHADOW needs BOTH signals to be acceptable (AND'd). When off, or when
    # no counterfactual engine is wired, the governor behaves exactly as before
    # (accuracy only).
    use_counterfactual_signal: bool = True
    # ACTIVE → SHADOW: marginal R per attributed trade below this (with the
    # engine's ``better_off_without`` flag set) triggers shadow on the
    # attribution signal alone.
    marginal_r_shadow_threshold: float = -0.05
    # SHADOW → ACTIVE: marginal R per attributed trade must be at/above this for
    # the counterfactual half of the reactivation AND-gate to pass.
    marginal_r_reactivation_threshold: float = 0.0
    # Minimum attributed trades before the counterfactual signal is trusted.
    marginal_r_min_trades: int = 100

    def __post_init__(self) -> None:
        for name in ("shadow_threshold", "reactivation_threshold", "disable_threshold"):
            v = float(getattr(self, name))
            if not (0.0 <= v <= 1.0):
                raise ValueError(
                    f"ModuleGovernorConfig.{name} must be in [0, 1], got {v!r}"
                )
        if float(self.disable_threshold) > float(self.shadow_threshold):
            raise ValueError(
                "ModuleGovernorConfig.disable_threshold must be <= shadow_threshold, "
                f"got disable={self.disable_threshold} shadow={self.shadow_threshold}"
            )
        for name in ("shadow_lookback", "reactivation_min_signals", "disable_min_signals"):
            if int(getattr(self, name)) < 1:
                raise ValueError(
                    f"ModuleGovernorConfig.{name} must be >= 1, got {getattr(self, name)!r}"
                )
        if int(self.auto_retry_days) < 0:
            raise ValueError(
                "ModuleGovernorConfig.auto_retry_days must be >= 0, "
                f"got {self.auto_retry_days!r}"
            )
        if int(self.feedback_lookback) <= 0:
            raise ValueError(
                "ModuleGovernorConfig.feedback_lookback must be > 0, "
                f"got {self.feedback_lookback!r}"
            )
        if int(self.marginal_r_min_trades) < 1:
            raise ValueError(
                "ModuleGovernorConfig.marginal_r_min_trades must be >= 1, "
                f"got {self.marginal_r_min_trades!r}"
            )
        if float(self.marginal_r_reactivation_threshold) < float(
            self.marginal_r_shadow_threshold
        ):
            raise ValueError(
                "ModuleGovernorConfig.marginal_r_reactivation_threshold must be "
                ">= marginal_r_shadow_threshold, got "
                f"reactivation={self.marginal_r_reactivation_threshold} "
                f"shadow={self.marginal_r_shadow_threshold}"
            )


@dataclass
class PostCloseTrackerConfig:
    """Settings for the post-close MFE/MAE tracker (learning layer).

    After a trade closes it schedules forward price checks (T+5m, T+15m, …)
    from ENTRY and measures Maximum Favorable / Adverse Excursion, deriving
    signal-quality vs management-quality so the learning layer can tell a bad
    read apart from a stop placed too tight. Purely observational — it never
    changes a live decision. Defaults OFF so it stays dormant until enabled.
    """

    enabled: bool = False
    db_path: str = "data/trade_journal.db"
    check_intervals_minutes: list[int] = field(
        default_factory=lambda: [5, 15, 30, 60]
    )
    max_retries: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.check_intervals_minutes, list) or not all(
            isinstance(x, (int, float)) and x >= 0
            for x in self.check_intervals_minutes
        ):
            raise ValueError(
                "PostCloseTrackerConfig.check_intervals_minutes must be a list "
                f"of non-negative numbers, got {self.check_intervals_minutes!r}"
            )
        if int(self.max_retries) < 0:
            raise ValueError(
                "PostCloseTrackerConfig.max_retries must be >= 0, "
                f"got {self.max_retries!r}"
            )


@dataclass
class PairLearnerConfig:
    """Settings for the PairLearner's per-pair size multiplier (learning layer).

    The legacy multiplier collapsed a continuous observed win rate into four
    discrete buckets ({0.0 AVOID, 0.7 REDUCE, 0.8 insufficient, 1.0 TRADE}), so
    a 56%% pair and a 75%% pair both mapped to 1.0. This config drives a smooth
    sigmoid replacement plus an optional entry-vs-management split (so a pair
    with good signals but poor trade management is not over-penalised).

    ``continuous_pair_multiplier`` defaults ON (the smooth sigmoid is the live
    behaviour). Set it to False to fall back to the legacy 4-bucket multiplier,
    in which case every field below is ignored.
    """

    # Master switch for the smooth multiplier. False = legacy 4-bucket.
    continuous_pair_multiplier: bool = True
    # Sigmoid shape: midpoint is the win rate that maps near the curve centre,
    # steepness controls how sharply it ramps, floor/ceiling bound the output.
    continuous_midpoint: float = 0.50
    continuous_steepness: float = 10.0
    continuous_floor: float = 0.3
    continuous_ceiling: float = 1.2
    # Bayesian shrinkage: thin samples are blended toward this prior; a sample
    # at/above ``shrinkage_full_weight`` trades uses the raw curve directly.
    continuous_prior: float = 0.8
    shrinkage_full_weight: int = 30
    # Hard lower bound so a degenerate value can never divide-by-zero downstream.
    continuous_absolute_floor: float = 0.1
    # Multiplier for pairs with too little history to size on (cold start) and
    # the trade count below which a pair is treated as cold start.
    cold_start_multiplier: float = 0.8
    cold_start_min_trades: int = 5

    # Entry-vs-management split (consumes PostCloseTracker signal accuracy).
    # When enabled the effective win rate the sigmoid sees is blended toward the
    # pair's entry (signal) accuracy, so a pair that reads the market well but is
    # managed poorly is not avoided for a problem the entry signal didn't cause.
    entry_management_split_enabled: bool = False
    entry_accuracy_blend_weight: float = 0.3
    # Record/log the per-pair management-quality breakdown during learning.
    management_quality_log_enabled: bool = True

    def __post_init__(self) -> None:
        if not (0.0 <= float(self.continuous_midpoint) <= 1.0):
            raise ValueError(
                "PairLearnerConfig.continuous_midpoint must be in [0, 1], "
                f"got {self.continuous_midpoint!r}"
            )
        if float(self.continuous_steepness) <= 0:
            raise ValueError(
                "PairLearnerConfig.continuous_steepness must be > 0, "
                f"got {self.continuous_steepness!r}"
            )
        if float(self.continuous_floor) > float(self.continuous_ceiling):
            raise ValueError(
                "PairLearnerConfig.continuous_floor must be <= continuous_ceiling, "
                f"got {self.continuous_floor} > {self.continuous_ceiling}"
            )
        if int(self.shrinkage_full_weight) <= 0:
            raise ValueError(
                "PairLearnerConfig.shrinkage_full_weight must be > 0, "
                f"got {self.shrinkage_full_weight!r}"
            )
        if float(self.continuous_absolute_floor) < 0:
            raise ValueError(
                "PairLearnerConfig.continuous_absolute_floor must be >= 0, "
                f"got {self.continuous_absolute_floor!r}"
            )
        if not (0.0 <= float(self.entry_accuracy_blend_weight) <= 1.0):
            raise ValueError(
                "PairLearnerConfig.entry_accuracy_blend_weight must be in [0, 1], "
                f"got {self.entry_accuracy_blend_weight!r}"
            )
        if int(self.cold_start_min_trades) < 0:
            raise ValueError(
                "PairLearnerConfig.cold_start_min_trades must be >= 0, "
                f"got {self.cold_start_min_trades!r}"
            )


@dataclass
class TunerAgentConfig:
    """Central coordinator for ALL auto-tuning (the "Tuner Agent").

    Every tunable component (ScoreOptimizer, RegimeLearner, PairLearner,
    SessionLearner, EVEstimator, GateTuner, planner Calibrator, SignalLedger
    grading) registers with one agent. The agent resolves dependency order,
    decides who is due, executes them in order, validates each result against
    the component's own safety bounds, rolls back on failure, and writes every
    action to a persistent audit log.

    Defaults ON: the agent is the sole tuning authority — every tunable
    registers with it, it resolves order/cadence and validates each result,
    and the legacy scattered tuning triggers are blocked while it runs.
    """

    # Master switch. When False, the legacy scattered tuning calls run as-is.
    enabled: bool = True
    # Soft per-tunable duration budget; an overrun is logged loudly (a running
    # sync tune cannot be safely hard-killed mid-flight without risking a
    # half-written DB).
    max_tune_duration_seconds: float = 30.0
    # Disable a tunable after this many consecutive failures (loud warning).
    max_consecutive_failures: int = 3
    # SQLite audit DB (under data/, gitignored).
    audit_db_path: str = "data/tuner_audit.db"
    # Verbose: also audit/log when should_tune returns False (debugging only).
    log_all_skips: bool = False

    def __post_init__(self) -> None:
        if float(self.max_tune_duration_seconds) <= 0:
            raise ValueError(
                "TunerAgentConfig.max_tune_duration_seconds must be > 0, "
                f"got {self.max_tune_duration_seconds!r}"
            )
        if int(self.max_consecutive_failures) < 1:
            raise ValueError(
                "TunerAgentConfig.max_consecutive_failures must be >= 1, "
                f"got {self.max_consecutive_failures!r}"
            )


@dataclass
class CounterfactualConfig:
    """Settings for the Counterfactual Attribution engine (L4).

    For every closed trade the engine holds the exact vote panel + consensus
    config that opened it, then replays that math with one module removed at a
    time (leave-one-out) to measure each module's MARGINAL contribution — which
    trades only happened *because* of it, and what those trades returned. The
    output ranks the modules by net contribution so the system can see who is
    helping and who is hurting from real production decisions.

    Purely analytical — it never changes a weight, mode, or decision. The
    leave-one-out pass is expensive, so it runs periodically (every
    ``attribution_interval`` closed trades) over the most recent
    ``attribution_lookback`` trades and the result is cached for the dashboard.
    Defaults OFF so it stays dormant until enabled.
    """

    # Master switch: capture per-trade decision snapshots + run attribution.
    counterfactual_enabled: bool = True
    # How many recent closed trades each attribution pass analyses.
    attribution_lookback: int = 500
    # Recompute the attribution table every N closed trades.
    attribution_interval: int = 100
    # Minimum closed trades before any attribution is computed (avoid noise).
    min_trades_for_attribution: int = 50
    # SQLite path (under data/, gitignored).
    counterfactual_db_path: str = "data/counterfactual.db"

    def __post_init__(self) -> None:
        if int(self.attribution_lookback) < 1:
            raise ValueError(
                "CounterfactualConfig.attribution_lookback must be >= 1, "
                f"got {self.attribution_lookback!r}"
            )
        if int(self.attribution_interval) < 1:
            raise ValueError(
                "CounterfactualConfig.attribution_interval must be >= 1, "
                f"got {self.attribution_interval!r}"
            )
        if int(self.min_trades_for_attribution) < 1:
            raise ValueError(
                "CounterfactualConfig.min_trades_for_attribution must be >= 1, "
                f"got {self.min_trades_for_attribution!r}"
            )


@dataclass
class InteractionConfig:
    """Settings for the Module Interaction Discovery engine (L5b).

    Extends the L4 leave-ONE-out attribution to leave-K-out: it replays the same
    stored decision snapshots with module SUBSETS removed to measure non-additive
    interactions. For every module pair it compares the joint removal delta to the
    sum of the individual removal deltas — the remainder is the interaction effect
    (SYNERGY when above ``synergy_threshold``, TOXIC when below ``toxic_threshold``).
    It also searches for the active-module subset that would have maximised the
    book and surfaces toxic / synergistic pairs as read-only recommendations.

    Purely analytical — it never changes a weight, mode, or decision. The
    leave-K-out replay is expensive (``2^M`` subsets over the lookback window), so
    it runs periodically (every ``interaction_interval`` closed trades) over the
    most recent ``interaction_lookback`` trades and the result is cached for the
    dashboard. It reuses the Counterfactual engine's stored trade snapshots, so it
    is only active when ``counterfactual_enabled`` is also on. Defaults OFF.
    """

    # Master switch: compute + cache the leave-K-out interaction analysis.
    interaction_discovery_enabled: bool = True
    # How many recent closed trades each analysis pass replays.
    interaction_lookback: int = 500
    # Recompute the interaction matrix every N closed trades.
    interaction_interval: int = 500
    # Interaction effect below this = toxic pair (reinforce each other's mistakes).
    toxic_threshold: float = -0.05
    # Interaction effect above this = synergistic pair (better than sum of parts).
    synergy_threshold: float = 0.05
    # Above this many voting modules, fall back to greedy subset search (the
    # exhaustive 2^M sweep is only run at or below this count).
    exhaustive_search_max_modules: int = 12
    # SQLite path (under data/, gitignored).
    interaction_db_path: str = "data/interaction_discovery.db"

    def __post_init__(self) -> None:
        if int(self.interaction_lookback) < 1:
            raise ValueError(
                "InteractionConfig.interaction_lookback must be >= 1, "
                f"got {self.interaction_lookback!r}"
            )
        if int(self.interaction_interval) < 1:
            raise ValueError(
                "InteractionConfig.interaction_interval must be >= 1, "
                f"got {self.interaction_interval!r}"
            )
        if int(self.exhaustive_search_max_modules) < 1:
            raise ValueError(
                "InteractionConfig.exhaustive_search_max_modules must be >= 1, "
                f"got {self.exhaustive_search_max_modules!r}"
            )
        if float(self.toxic_threshold) > float(self.synergy_threshold):
            raise ValueError(
                "InteractionConfig.toxic_threshold must be <= synergy_threshold, "
                f"got toxic={self.toxic_threshold!r} synergy={self.synergy_threshold!r}"
            )


@dataclass
class ParameterEvolutionConfig:
    """Settings for the Parameter Evolution engine (L5a).

    The TunerAgent follows gradients on existing parameters; this engine
    *explores* — it generates candidate values for the consensus / ranker
    thresholds, replays them over recent closed-trade snapshots (the same
    decision-replay the Counterfactual engine uses), walk-forward validates the
    winners, then proves them over live closes (shadow validation) before
    *recommending* a promotion. It never mutates live config directly — an
    injected callback (gated by the wiring layer) applies an approved value, so
    the TunerAgent stays the sole tuning authority.

    Defaults OFF so it stays dormant until enabled. When on it gracefully waits
    for ``min_replay_trades`` closed snapshots before doing anything.
    """

    # Master switch: explore + shadow-validate parameter candidates.
    param_evolution_enabled: bool = True
    # How many candidate values to generate per parameter each tournament.
    candidates_per_param: int = 10
    # How many recent closed trades each replay tournament analyses.
    replay_lookback: int = 500
    # Live closes a candidate must prove itself over before a promotion.
    shadow_validation_trades: int = 50
    # Minimum R improvement per trade for a candidate to qualify / promote.
    significance_threshold: float = 0.05
    # Train/test split for the walk-forward overfitting guard (0.5–0.95).
    walk_forward_split: float = 0.7
    # Minimum hours between successive promotions (rate limiting).
    evolution_cooldown_hours: float = 48.0
    # Maximum candidates in shadow validation at once.
    max_concurrent_shadows: int = 3
    # Trades to watch a promoted change before a rollback recommendation.
    rollback_window: int = 100
    # Minimum closed snapshots before any tournament runs.
    min_replay_trades: int = 50
    # SQLite path (under data/, gitignored).
    param_evolution_db_path: str = "data/param_evolution.db"

    def __post_init__(self) -> None:
        if int(self.candidates_per_param) < 2:
            raise ValueError(
                "ParameterEvolutionConfig.candidates_per_param must be >= 2, "
                f"got {self.candidates_per_param!r}"
            )
        if int(self.shadow_validation_trades) < 1:
            raise ValueError(
                "ParameterEvolutionConfig.shadow_validation_trades must be >= 1, "
                f"got {self.shadow_validation_trades!r}"
            )
        if not (0.5 <= float(self.walk_forward_split) <= 0.95):
            raise ValueError(
                "ParameterEvolutionConfig.walk_forward_split must be in [0.5, 0.95], "
                f"got {self.walk_forward_split!r}"
            )
        if float(self.significance_threshold) < 0:
            raise ValueError(
                "ParameterEvolutionConfig.significance_threshold must be >= 0, "
                f"got {self.significance_threshold!r}"
            )
        if int(self.max_concurrent_shadows) < 1:
            raise ValueError(
                "ParameterEvolutionConfig.max_concurrent_shadows must be >= 1, "
                f"got {self.max_concurrent_shadows!r}"
            )


@dataclass
class SignalDiscoveryConfig:
    """Settings for the Synthetic Signal Discovery engine (L5c).

    The system combines module votes via fixed consensus logic. This engine
    mines the recorded vote panels + realised outcomes for *combinations* of
    module-direction conditions (e.g. "structure LONG AND liquidity LONG AND
    momentum absent") whose win-rate / expectancy edge persists out-of-sample —
    rules nobody wrote, found in the data. It guards against overfitting with a
    train/test split and a minimum-support floor, and only *recommends*
    candidate rules. It never auto-creates a live signal. Defaults OFF.
    """

    # Master switch: mine + OOS-validate candidate signal rules.
    signal_discovery_enabled: bool = True
    # How many recent closed trades each mining pass uses.
    discovery_lookback: int = 1000
    # Recompute every N closed trades.
    discovery_interval: int = 200
    # Minimum closed trades before mining runs.
    min_trades_for_discovery: int = 100
    # Minimum trades a rule must fire on (support) to be considered.
    min_rule_support: int = 15
    # Maximum module-conditions in a discovered rule (combinatorial guard).
    max_rule_conditions: int = 3
    # Minimum expectancy edge (R/trade) over baseline for a rule to qualify.
    min_edge_r: float = 0.10
    # Train/test split for out-of-sample validation (0.5–0.95).
    discovery_walk_forward_split: float = 0.7
    # SQLite path (under data/, gitignored).
    signal_discovery_db_path: str = "data/signal_discovery.db"

    # ── Overfitting protection (L5c hardening) ──
    # Family-wise significance: a rule qualifies only when its fire-vs-rest edge
    # clears a Bonferroni-adjusted level (alpha / number of candidates tested),
    # so mining many combinations does not surface chance "edges".
    bonferroni_alpha: float = 0.05
    # Out-of-sample hurdle: the test-half edge must retain at least this fraction
    # of the train-half edge (guards against in-sample-only flukes).
    walk_forward_ratio_threshold: float = 0.6
    # Per-recompute multiplicative score decay; a rule that stops re-confirming
    # fades out instead of lingering as an active candidate.
    score_decay_rate: float = 0.05
    # Hard cap on how many discovered rules are flagged ACTIVE at once.
    max_active_signals: int = 5

    # ── Virtual voting modules (L5c shadow → promote → retire lifecycle) ──
    # Master switch for the LIVE promotion pipeline: when on, qualifying
    # discovered rules are registered as virtual voting modules (always in
    # SHADOW first), promoted to ACTIVE once they earn it, and retired when they
    # degrade. Defaults OFF — discovery stays purely advisory until flipped on.
    # NOTE: ``signal_discovery_enabled`` is the kill switch on top of this — when
    # it is off, every virtual module is forced to weight 0.0 regardless.
    virtual_promotion_enabled: bool = True
    # Trades a module must spend in SHADOW (since registration) before it is
    # eligible for promotion.
    shadow_trades_required: int = 50
    # Minimum graded accuracy (direction-correct rate) to promote a shadow.
    min_shadow_accuracy: float = 0.52
    # Minimum marginal R per attributed trade to promote (non-binding at 0.0 —
    # a shadow module typically has no attribution yet).
    min_shadow_marginal_r: float = 0.0
    # Minimum graded signals before a shadow module's accuracy is trusted.
    promotion_min_signals: int = 20
    # Hard rate limit on promotions per lifecycle evaluation.
    max_promotions_per_cycle: int = 1
    # Initial vote weight assigned on promotion (grows/shrinks via lifecycle).
    promotion_initial_weight: float = 1.0
    # On restart, ACTIVE modules re-enter a supervised shadow window for this
    # many trades before resuming live weight (stale-signal safety).
    restart_shadow_trades: int = 10
    # Retirement: an ACTIVE module is DISABLED when its accuracy falls below this
    # over at least ``retirement_min_signals`` graded signals.
    retirement_accuracy_threshold: float = 0.48
    retirement_min_signals: int = 30
    # How often (closed-trade cadence) the lifecycle evaluation runs.
    retirement_check_interval: int = 50
    # Retirement by marginal R: harmful (better-off-without) below threshold,
    # trusted only once this many attributed trades exist.
    retirement_marginal_r_min_trades: int = 50
    retirement_marginal_r_threshold: float = -0.05
    # Lookback (graded signals) used when reading a virtual module's accuracy.
    feedback_lookback: int = 500

    def __post_init__(self) -> None:
        if int(self.discovery_lookback) < 1:
            raise ValueError(
                "SignalDiscoveryConfig.discovery_lookback must be >= 1, "
                f"got {self.discovery_lookback!r}"
            )
        if int(self.min_rule_support) < 1:
            raise ValueError(
                "SignalDiscoveryConfig.min_rule_support must be >= 1, "
                f"got {self.min_rule_support!r}"
            )
        if not (1 <= int(self.max_rule_conditions) <= 5):
            raise ValueError(
                "SignalDiscoveryConfig.max_rule_conditions must be in [1, 5], "
                f"got {self.max_rule_conditions!r}"
            )
        if not (0.5 <= float(self.discovery_walk_forward_split) <= 0.95):
            raise ValueError(
                "SignalDiscoveryConfig.discovery_walk_forward_split must be in "
                f"[0.5, 0.95], got {self.discovery_walk_forward_split!r}"
            )
        if not (0.0 < float(self.bonferroni_alpha) <= 1.0):
            raise ValueError(
                "SignalDiscoveryConfig.bonferroni_alpha must be in (0, 1], "
                f"got {self.bonferroni_alpha!r}"
            )
        if not (0.0 <= float(self.walk_forward_ratio_threshold) <= 1.0):
            raise ValueError(
                "SignalDiscoveryConfig.walk_forward_ratio_threshold must be in "
                f"[0, 1], got {self.walk_forward_ratio_threshold!r}"
            )
        if not (0.0 <= float(self.score_decay_rate) <= 1.0):
            raise ValueError(
                "SignalDiscoveryConfig.score_decay_rate must be in [0, 1], "
                f"got {self.score_decay_rate!r}"
            )
        if int(self.max_active_signals) < 0:
            raise ValueError(
                "SignalDiscoveryConfig.max_active_signals must be >= 0, "
                f"got {self.max_active_signals!r}"
            )
        if not (0.0 <= float(self.min_shadow_accuracy) <= 1.0):
            raise ValueError(
                "SignalDiscoveryConfig.min_shadow_accuracy must be in [0, 1], "
                f"got {self.min_shadow_accuracy!r}"
            )
        if not (0.0 <= float(self.retirement_accuracy_threshold) <= 1.0):
            raise ValueError(
                "SignalDiscoveryConfig.retirement_accuracy_threshold must be in "
                f"[0, 1], got {self.retirement_accuracy_threshold!r}"
            )
        if int(self.shadow_trades_required) < 0:
            raise ValueError(
                "SignalDiscoveryConfig.shadow_trades_required must be >= 0, "
                f"got {self.shadow_trades_required!r}"
            )
        if int(self.max_promotions_per_cycle) < 0:
            raise ValueError(
                "SignalDiscoveryConfig.max_promotions_per_cycle must be >= 0, "
                f"got {self.max_promotions_per_cycle!r}"
            )
        if float(self.promotion_initial_weight) < 0:
            raise ValueError(
                "SignalDiscoveryConfig.promotion_initial_weight must be >= 0, "
                f"got {self.promotion_initial_weight!r}"
            )
        if int(self.restart_shadow_trades) < 0:
            raise ValueError(
                "SignalDiscoveryConfig.restart_shadow_trades must be >= 0, "
                f"got {self.restart_shadow_trades!r}"
            )


@dataclass
class CapitalAllocationConfig:
    """Settings for the Capital Allocation Engine (L5.5a).

    Allocates capital across *strategy fingerprints* — the execution-style
    signature of a trade (entry mode × horizon, e.g. ``MARKET|SCALP`` vs
    ``PENDING|SWING``) — instead of picking a single "best" style. Each
    fingerprint's expectancy is scored across three trade horizons (recent /
    medium / long) with the long horizon dominating, so the book diversifies
    its alpha sources and never reinvents itself on a short winning streak.

    The raw allocations sum to 1.0 (a true portfolio split, shown on the
    dashboard). The *sizing multiplier* applied to a trade is the fingerprint's
    allocation normalised against the strongest fingerprint in the book and
    clamped to ``[min_allocation, 1.0]`` — purely de-risking, never amplifying.
    With insufficient history (< ``min_trades_for_scoring`` total closed trades)
    every fingerprint resolves to a 1.0 multiplier, i.e. behaviour is identical
    to the pre-allocator system.

    Anti-thrashing: allocations only recompute every ``rebalance_interval_trades``
    closed trades, and no single fingerprint's allocation may move more than
    ``max_allocation_shift`` per rebalance. A ``min_allocation`` floor keeps a
    proven-but-currently-cold style from being starved to zero.
    """

    # Master switch — ACTIVE by default. When off the engine never sizes.
    enabled: bool = True

    # Three-horizon expectancy windows (most-recent N closed trades per
    # fingerprint) and their blend weights (long horizon dominates).
    short_horizon_trades: int = 50
    medium_horizon_trades: int = 500
    long_horizon_trades: int = 5000
    short_weight: float = 0.2
    medium_weight: float = 0.3
    long_weight: float = 0.5

    # Recompute allocations only every N closed trades (anti-thrash cadence).
    rebalance_interval_trades: int = 25
    # Max fraction a single fingerprint's allocation may move per rebalance.
    max_allocation_shift: float = 0.10
    # Allocation floor — no active style is starved below this share.
    min_allocation: float = 0.05
    # Below this many total closed trades the sizing multiplier is a 1.0 no-op.
    min_trades_for_scoring: int = 50
    # Bayesian shrinkage strength — prior (book-average) weight in trades.
    bayesian_prior_trades: int = 100
    # Softmax temperature mapping blended expectancy → allocation weight.
    allocation_temperature: float = 0.5
    # SQLite path (under data/, gitignored).
    capital_allocation_db_path: str = "data/capital_allocation.db"

    def __post_init__(self) -> None:
        for name in (
            "short_horizon_trades",
            "medium_horizon_trades",
            "long_horizon_trades",
            "rebalance_interval_trades",
            "min_trades_for_scoring",
            "bayesian_prior_trades",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(
                    f"CapitalAllocationConfig.{name} must be >= 1, "
                    f"got {getattr(self, name)!r}"
                )
        if not (0.0 < float(self.max_allocation_shift) <= 1.0):
            raise ValueError(
                "CapitalAllocationConfig.max_allocation_shift must be in (0, 1], "
                f"got {self.max_allocation_shift!r}"
            )
        if not (0.0 <= float(self.min_allocation) < 1.0):
            raise ValueError(
                "CapitalAllocationConfig.min_allocation must be in [0, 1), "
                f"got {self.min_allocation!r}"
            )
        if float(self.allocation_temperature) <= 0.0:
            raise ValueError(
                "CapitalAllocationConfig.allocation_temperature must be > 0, "
                f"got {self.allocation_temperature!r}"
            )


@dataclass
class ExecutionProfileConfig:
    """Settings for the Execution Style Profiles engine (L5.5b).

    L5.5a allocates capital across execution-style *fingerprints*. This layer
    supplies the execution style itself: instead of one fixed parameter set for
    every trade, the system selects a named **Execution Profile** — a complete
    parameter vector (SL distance, TP R:R, trailing method/activation, partial
    rules, min-score) — per trade based on market context (the ranker horizon,
    the regime, and consensus strength).

    Profiles are NOT hard-coded strategies. They are parameter vectors persisted
    in SQLite that the evolution layer can tune, create, retire, score (via the
    capital allocator's per-profile fingerprint) and shadow/disable (governor).
    With profiles disabled — or when no profile matches — the pipeline falls
    back to the existing config-level defaults, so behaviour is identical to the
    pre-profile system (true no-op).

    Selection is purely additive: a profile only ever overrides SL/TP/min-score
    in the entry engine and BE/trailing/partial in the trade manager via the
    SAME per-trade override hooks the planner already uses — it never changes
    direction, never forces a trade, never bypasses a hard risk veto. A trade
    Planner override always takes precedence over a profile default.
    """

    # Master switch — ACTIVE by default. When off, no profile is selected and
    # the entry engine / trade manager use their config-level defaults.
    enabled: bool = True
    # Fallback profile when no profile matches the trade context (must be one of
    # the seeded built-ins or a created profile).
    default_profile: str = "standard_swing"
    # Let Parameter Evolution create new profiles (new parameter vectors). When
    # off, only the seeded built-ins and the tunable scalar knobs are mutable.
    allow_profile_creation: bool = True
    # Hard cap on simultaneously active profiles (guards profile-creation churn).
    max_active_profiles: int = 10
    # Minimum closed trades carrying a profile before it can be scored / shown
    # as "evaluated" on the dashboard.
    min_trades_for_scoring: int = 30
    # Consensus-strength band (result.score / 100) used by selection refinement.
    # A weak panel biases toward tighter profiles; a strong one toward wider.
    strong_consensus_threshold: float = 0.75
    weak_consensus_threshold: float = 0.45
    # SQLite path (under data/, gitignored).
    execution_profiles_db_path: str = "data/execution_profiles.db"

    def __post_init__(self) -> None:
        if int(self.max_active_profiles) < 1:
            raise ValueError(
                "ExecutionProfileConfig.max_active_profiles must be >= 1, "
                f"got {self.max_active_profiles!r}"
            )
        if int(self.min_trades_for_scoring) < 1:
            raise ValueError(
                "ExecutionProfileConfig.min_trades_for_scoring must be >= 1, "
                f"got {self.min_trades_for_scoring!r}"
            )
        for name in ("strong_consensus_threshold", "weak_consensus_threshold"):
            v = float(getattr(self, name))
            if not (0.0 <= v <= 1.0):
                raise ValueError(
                    f"ExecutionProfileConfig.{name} must be in [0, 1], got {v!r}"
                )
        if float(self.weak_consensus_threshold) > float(self.strong_consensus_threshold):
            raise ValueError(
                "ExecutionProfileConfig.weak_consensus_threshold must be <= "
                "strong_consensus_threshold"
            )
        if not str(self.default_profile).strip():
            raise ValueError("ExecutionProfileConfig.default_profile must be non-empty")


@dataclass
class RegimeDetectionConfig:
    """Settings for the Regime Detection Engine (L7).

    Names the current market regime per pair — TRENDING_UP / TRENDING_DOWN /
    RANGING / VOLATILE / QUIET / UNKNOWN — from rule-based, pure-Python price
    signals (directional strength, volatility ratio, mean-reversion, range
    compression). The regime is *context* every other adaptive layer can read
    (execution profiles, capital allocator, vote calibrator, behaviour
    discovery, governor); it never directs or blocks a trade. With no history a
    pair resolves to UNKNOWN at zero confidence, so behaviour is identical to
    the pre-L7 system until evidence exists.

    ``hysteresis_bars`` consecutive agreeing observations are required before the
    committed regime flips, so a single spike never re-labels the market.
    """

    # Master switch — ACTIVE by default. When off the detector is a pure no-op.
    enabled: bool = True

    # Bars of close history used per classification.
    lookback_bars: int = 50
    # Consecutive agreeing observations required to commit a regime flip.
    hysteresis_bars: int = 5
    # Volatility-ratio windows (short stdev / long stdev).
    volatility_short_window: int = 10
    volatility_long_window: int = 50
    # ADX-like directional-strength smoothing period and autocorrelation lag.
    adx_period: int = 14
    autocorrelation_lag: int = 1
    # Classification thresholds (all in [0, 1]).
    trending_threshold: float = 0.6
    volatile_threshold: float = 0.7
    quiet_threshold: float = 0.3
    # SQLite path (under data/, gitignored).
    regime_detection_db_path: str = "data/regime_detection.db"

    def __post_init__(self) -> None:
        for name in (
            "lookback_bars",
            "hysteresis_bars",
            "volatility_short_window",
            "volatility_long_window",
            "adx_period",
            "autocorrelation_lag",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(
                    f"RegimeDetectionConfig.{name} must be >= 1, "
                    f"got {getattr(self, name)!r}"
                )
        if int(self.volatility_long_window) <= int(self.volatility_short_window):
            raise ValueError(
                "RegimeDetectionConfig.volatility_long_window must be > "
                "volatility_short_window"
            )
        for name in ("trending_threshold", "volatile_threshold", "quiet_threshold"):
            v = float(getattr(self, name))
            if not (0.0 <= v <= 1.0):
                raise ValueError(
                    f"RegimeDetectionConfig.{name} must be in [0, 1], got {v!r}"
                )


@dataclass
class RiskManagementConfig:
    """Settings for the Risk Management Layer (L8).

    The circuit-breaker layer: drawdown limits, correlated-exposure caps, and
    overconcentration limits behind a single gate. It is the only adaptive
    component that can *block* a trade, and only ever on a hard risk limit
    (never on signal quality); every block is logged and persisted. With no
    history / disabled it passes everything and the sizing factor is 1.0, so the
    system behaves identically to the pre-L8 pipeline.
    """

    # Master switch — ACTIVE by default. When off the gate passes everything.
    enabled: bool = True

    # Drawdown limits (positive percentages).
    daily_drawdown_limit_pct: float = 3.0      # halt new trades for the day
    rolling_drawdown_limit_pct: float = 8.0    # enter sizing cooldown
    hard_stop_drawdown_pct: float = 15.0       # halt everything + flatten signal
    cooldown_hours: float = 4.0                # cooldown duration after rolling breach
    cooldown_sizing_factor: float = 0.5        # sizing multiplier while in cooldown

    # Exposure caps.
    max_simultaneous_positions: int = 10
    max_per_pair_positions: int = 2
    max_directional_exposure_pct: float = 60.0
    max_per_regime_pct: float = 40.0

    # Correlation risk.
    correlation_threshold: float = 0.7
    max_correlated_exposure_factor: float = 1.5
    correlation_lookback_bars: int = 100
    correlation_update_interval: int = 50

    # SQLite path (under data/, gitignored).
    risk_management_db_path: str = "data/risk_management.db"

    def __post_init__(self) -> None:
        for name in (
            "daily_drawdown_limit_pct",
            "rolling_drawdown_limit_pct",
            "hard_stop_drawdown_pct",
        ):
            if float(getattr(self, name)) <= 0.0:
                raise ValueError(
                    f"RiskManagementConfig.{name} must be > 0, "
                    f"got {getattr(self, name)!r}"
                )
        if not (
            self.daily_drawdown_limit_pct
            <= self.rolling_drawdown_limit_pct
            <= self.hard_stop_drawdown_pct
        ):
            raise ValueError(
                "RiskManagementConfig drawdown limits must satisfy "
                "daily <= rolling <= hard_stop"
            )
        if not (0.0 < float(self.cooldown_sizing_factor) <= 1.0):
            raise ValueError(
                "RiskManagementConfig.cooldown_sizing_factor must be in (0, 1], "
                f"got {self.cooldown_sizing_factor!r}"
            )
        for name in ("max_simultaneous_positions", "max_per_pair_positions",
                     "correlation_lookback_bars", "correlation_update_interval"):
            if int(getattr(self, name)) < 1:
                raise ValueError(
                    f"RiskManagementConfig.{name} must be >= 1, "
                    f"got {getattr(self, name)!r}"
                )
        if not (0.0 <= float(self.correlation_threshold) <= 1.0):
            raise ValueError(
                "RiskManagementConfig.correlation_threshold must be in [0, 1], "
                f"got {self.correlation_threshold!r}"
class BehaviorDiscoveryConfig:
    """Settings for the Behaviour Discovery engine (L6).

    L5 discovers signal *combinations*; this layer discovers execution
    *behaviours*. It records each closed trade's execution feature vector
    (entry mode, horizon, the chosen profile's SL/TP shape, regime, consensus
    strength, conviction, score, time-of-day, volatility, instrument class,
    direction) + realised R, then periodically clusters those vectors
    (density-based, no pre-set ``k``, no heavy ML) into emergent **Behaviours**.

    Each behaviour is scored (win-rate / expectancy / Sharpe-like) with Bayesian
    shrinkage toward the book average and walked through a SHADOW → ACTIVE →
    RETIRED lifecycle, rate-limited by a per-behaviour cooldown so it never
    thrashes. Purely advisory + observational — it never changes a weight, mode,
    or decision; it surfaces what the data shows for the evolution stack (and a
    human) to adopt.

    Dormant by construction: below ``min_trades_to_cluster`` recorded trades
    nothing clusters and every accessor returns an empty shape (a true no-op).
    """

    # Master switch — ACTIVE by default. When off, nothing is recorded/clustered.
    behavior_discovery_enabled: bool = True
    # How many recent closed trades each clustering pass uses.
    behavior_lookback: int = 1000
    # Minimum recorded trades before any clustering runs.
    min_trades_to_cluster: int = 100
    # Minimum trades in a density cluster for it to be a behaviour.
    min_cluster_size: int = 20
    # Hard cap on simultaneously tracked behaviours (largest clusters kept).
    max_clusters: int = 15
    # Recompute the clustering every N recorded trades.
    recluster_every_n_trades: int = 50
    # Neighbour radius (normalised [0,1] feature distance) for density clustering.
    cluster_eps: float = 0.25
    # Bayesian shrinkage strength — prior (book-average) weight in trades.
    bayesian_prior_trades: int = 50
    # Shrunk-expectancy percentile to promote SHADOW → ACTIVE.
    promote_threshold: float = 0.65
    # Shrunk-expectancy percentile to retire ACTIVE → RETIRED.
    retire_threshold: float = 0.30
    # Minimum recorded trades between lifecycle transitions per behaviour.
    cooldown_trades: int = 100
    # Centroid distance under which a fresh cluster is matched to an existing
    # behaviour (lifecycle continuity across recompute passes).
    centroid_match_eps: float = 0.20
    # SQLite path (under data/, gitignored).
    behavior_discovery_db_path: str = "data/behavior_discovery.db"

    def __post_init__(self) -> None:
        for name in (
            "behavior_lookback",
            "min_trades_to_cluster",
            "min_cluster_size",
            "max_clusters",
            "recluster_every_n_trades",
            "bayesian_prior_trades",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(
                    f"BehaviorDiscoveryConfig.{name} must be >= 1, "
                    f"got {getattr(self, name)!r}"
                )
        if int(self.min_cluster_size) < 2:
            raise ValueError(
                "BehaviorDiscoveryConfig.min_cluster_size must be >= 2, "
                f"got {self.min_cluster_size!r}"
            )
        if not (0.0 < float(self.cluster_eps) <= 1.0):
            raise ValueError(
                "BehaviorDiscoveryConfig.cluster_eps must be in (0, 1], "
                f"got {self.cluster_eps!r}"
            )
        for name in ("promote_threshold", "retire_threshold"):
            v = float(getattr(self, name))
            if not (0.0 <= v <= 1.0):
                raise ValueError(
                    f"BehaviorDiscoveryConfig.{name} must be in [0, 1], got {v!r}"
                )
        if float(self.retire_threshold) > float(self.promote_threshold):
            raise ValueError(
                "BehaviorDiscoveryConfig.retire_threshold must be <= "
                "promote_threshold"
            )
        if int(self.cooldown_trades) < 0:
            raise ValueError(
                "BehaviorDiscoveryConfig.cooldown_trades must be >= 0, "
                f"got {self.cooldown_trades!r}"
            )
        if not (0.0 < float(self.centroid_match_eps) <= 1.0):
            raise ValueError(
                "BehaviorDiscoveryConfig.centroid_match_eps must be in (0, 1], "
                f"got {self.centroid_match_eps!r}"
            )


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
    opportunity_ranker: OpportunityRankerConfig = field(default_factory=OpportunityRankerConfig)
    decision_trace: DecisionTraceConfig = field(default_factory=DecisionTraceConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    outcome_feedback: OutcomeFeedbackConfig = field(default_factory=OutcomeFeedbackConfig)
    signal_ledger: SignalLedgerConfig = field(default_factory=SignalLedgerConfig)
    post_close_tracker: PostCloseTrackerConfig = field(default_factory=PostCloseTrackerConfig)
    pair_learner: PairLearnerConfig = field(default_factory=PairLearnerConfig)
    vote_calibrator: VoteCalibratorConfig = field(default_factory=VoteCalibratorConfig)
    module_governor: ModuleGovernorConfig = field(default_factory=ModuleGovernorConfig)
    tuner_agent: TunerAgentConfig = field(default_factory=TunerAgentConfig)
    counterfactual: CounterfactualConfig = field(default_factory=CounterfactualConfig)
    interaction: InteractionConfig = field(default_factory=InteractionConfig)
    param_evolution: ParameterEvolutionConfig = field(default_factory=ParameterEvolutionConfig)
    signal_discovery: SignalDiscoveryConfig = field(default_factory=SignalDiscoveryConfig)
    capital_allocation: CapitalAllocationConfig = field(default_factory=CapitalAllocationConfig)
    execution_profiles: ExecutionProfileConfig = field(default_factory=ExecutionProfileConfig)
    regime_detection: RegimeDetectionConfig = field(default_factory=RegimeDetectionConfig)
    risk_management: RiskManagementConfig = field(default_factory=RiskManagementConfig)
    behavior_discovery: BehaviorDiscoveryConfig = field(default_factory=BehaviorDiscoveryConfig)
    layered_decision: LayeredDecisionConfig = field(default_factory=LayeredDecisionConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    data_backup: DataBackupConfig = field(default_factory=DataBackupConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    governor: GovernorConfig = field(default_factory=GovernorConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)
    # NOTE: scan cadence is owned by PerformanceConfig.scan_interval_active /
    # scan_interval_with_positions (consumed by ScanScheduler). The old
    # AppConfig.scan_interval_seconds was superseded and never read — removed.
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

"""
APEX TRADER — Instrument Profile
The single source of truth for per-instrument analysis parameters.

Every detector, engine, and scanner pulls its tuning values from here
instead of using hardcoded numbers. Add a new symbol to config.py,
assign it a category — it immediately gets the right profile.

No symbol names are hardcoded here. Everything is driven by category
so the system scales to any number of instruments automatically.
"""

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from loguru import logger

from config import INSTRUMENT_REGISTRY

# Default per-session tuning tables (opportunistic-trader rewire). Defined here
# — not imported from brain.session_context — so this module stays free of a
# circular import (session_context imports get_profile from here). The same
# values are duplicated on EntryConfig and in session_context as ultimate
# fallbacks, exactly as the compression-detector defaults are.
_DEFAULT_SESSION_SIZE_MULTIPLIERS: dict[str, float] = {
    "ASIAN": 0.5, "LONDON": 1.2, "NY": 1.0, "LONDON_NY_OVERLAP": 1.3,
}
_DEFAULT_SESSION_ZONE_WEIGHTS: dict[str, float] = {
    "ASIAN": 0.8, "LONDON": 1.1, "NY": 1.0, "LONDON_NY_OVERLAP": 1.2,
}


@dataclass(frozen=True)
class InstrumentProfile:
    """
    All analysis parameters for one instrument category.
    Every field that was previously hardcoded in a detector or engine
    now lives here and is looked up per-symbol at runtime.
    """

    # ── Identity ──────────────────────────────────────────────────────
    category: str                       # "forex" / "commodity" / "index" / "synthetic" / "crypto"

    # ── Structure Engine ──────────────────────────────────────────────
    swing_lookback: int                 # Candles each side to confirm a swing point
    min_swing_size_pips: float          # Minimum swing size to count (filters noise)

    # ── FVG Detector ─────────────────────────────────────────────────
    fvg_proximity_pips: float           # How close price must be to an FVG to qualify
    fvg_min_size_pips: float            # Minimum gap size — smaller gaps are spread noise

    # ── Order Block Detector ──────────────────────────────────────────
    ob_buffer_pips: float               # Tolerance around OB edge for price-at-OB check
    ob_min_impulse_pips: float          # Minimum move size to qualify an OB

    # ── Entry Engine ──────────────────────────────────────────────────
    sl_buffer_pips: float               # Extra buffer beyond zone for stop loss placement
    min_risk_pips: float                # Minimum SL distance — below this, reject setup
    m1_confirmation_bars: int           # How many M1 bars to look back for CHoCH/BOS

    # ── Scoring & Filters ─────────────────────────────────────────────
    min_entry_score: int                # READY threshold — adjusts for available confluences
    wyckoff_enabled: bool               # Wyckoff meaningless on synthetics (no real volume)
    currency_strength_enabled: bool     # Only relevant for actual currency pairs
    news_filter_enabled: bool           # Synthetics don't react to economic news
    session_score_contribution: bool    # False for 24/7 instruments — session never penalises

    # ── MTF Confluence ────────────────────────────────────────────────
    mtf_overlap_threshold_pips: float   # Min overlap between M5 and M15 FVGs to count as confluence

    # ── Compression Detector ──────────────────────────────────────────
    # Per-instrument tuning for the global market-state classifier
    # (brain/compression_detector.py).  Defaulted so every existing profile
    # inherits sensible values; a category or symbol profile can override any
    # of them to suit an instrument's own volatility behaviour.  These are the
    # single source of truth — the detector reads the profile first and falls
    # back to the EntryConfig defaults only when a profile omits a field.
    compression_lookback: int = 100          # Rolling BBW-percentile window (bars)
    compression_threshold_pct: float = 15.0  # COMPRESSING = BBW in bottom Nth pctile
    adx_trending_threshold: float = 25.0     # TRENDING = ADX above this level
    expansion_threshold_pct: float = 70.0    # EXPANDING = BBW breaks above this pctile

    # ── Session Context ───────────────────────────────────────────────
    # Per-instrument tuning for the global session classifier
    # (brain/session_context.py).  Defaulted so every existing profile inherits
    # sensible values; a category or symbol profile can override either table to
    # calibrate an instrument's own session behaviour (e.g. Gold's London/NY
    # pattern).  ``session_size_multipliers`` scales position size per session;
    # ``session_zone_weights`` scales a zone's conviction per session.  The
    # session context reads the profile first and falls back to the EntryConfig
    # defaults only when a profile omits a table.
    session_size_multipliers: dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_SESSION_SIZE_MULTIPLIERS)
    )
    session_zone_weights: dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_SESSION_ZONE_WEIGHTS)
    )

    # ── VWAP-as-zone (prep, OFF by default) ───────────────────────────
    # When enabled, VWAP deviation bands would generate entry zones alongside
    # FVG/OB zones (see the TODO in entry/zone_watcher.py).  Default OFF — this
    # is prep work for a future PR, not an activated feature.
    vwap_zone_enabled: bool = False

    # ── Phase 2 Feature A: Stop-out flip ──────────────────────────────
    # When a trade is stopped out (SL hit) the system immediately evaluates a
    # flip into the opposite direction (using the orchestrator's live tick +
    # M5 flip confirmation). ``stopout_flip_cooldown_s`` is the minimum seconds
    # between flips on the same symbol; ``stopout_flip_max_per_zone`` caps how
    # many flips one zone may spawn before it is exhausted (whipsaw guard). The
    # detector reads the profile first, then the EntryConfig default.
    stopout_flip_enabled: bool = True
    stopout_flip_cooldown_s: float = 30.0
    stopout_flip_max_per_zone: int = 2

    # ── Phase 2 Feature B: Pre-staged limit orders ────────────────────
    # When a zone is active and price is within ``staging_proximity_pips`` of a
    # zone boundary, a pending LIMIT order is staged at the boundary instead of
    # waiting for an M1-close market order. ``staging_cooldown_s`` throttles
    # re-staging to prevent stage/cancel flapping. ``pre_staging_enabled`` is
    # OFF by default — a profile opts in (Gold does).
    pre_staging_enabled: bool = False
    staging_proximity_pips: float = 5.0
    staging_cooldown_s: float = 60.0

    # ── Phase 2 Feature C: Active compression / session filtering ──────
    # When the compression detector reads COMPRESSING the zone conviction is
    # boosted by ``compression_conviction_boost``; EXPANDING boosts by
    # ``expansion_conviction_boost`` (both multiply conviction before the score
    # gate). A COMPRESSING + ASIAN combination is treated as dangerous chop —
    # the entry is skipped unless conviction exceeds
    # ``asian_compression_min_conviction``.
    compression_conviction_boost: float = 1.2
    expansion_conviction_boost: float = 1.5
    asian_compression_min_conviction: int = 80

    # ── Phase 2 Feature D: DXY correlation filter ─────────────────────
    # For USD-denominated instruments (XAUUSD, forex USD pairs) a USD strength
    # read moving AGAINST the trade (USD strengthening while LONG gold, or
    # weakening while SHORT gold) emits a warning vote that reduces conviction
    # by ``dxy_opposition_penalty`` (a penalty, never a hard block).
    # ``dxy_lookback_bars`` is the correlation lookback window passed to the
    # strength reader.
    dxy_filter_enabled: bool = True
    dxy_opposition_penalty: float = 0.15
    dxy_lookback_bars: int = 20

    # ── Phase 3 Feature A: News calendar pre-planning ─────────────────
    # For scheduled high-impact events (CPI/NFP/FOMC) the news planner stages a
    # BUY_STOP above the current range and a SELL_STOP below it, OCO-style (one
    # fills → the opposite is cancelled). ``news_pre_planning_enabled`` is OFF by
    # default — a profile opts in (Gold does). ``news_pre_stage_minutes`` is how
    # many minutes before the event the breakout orders are staged;
    # ``news_range_lookback_bars`` is how many recent candles define the range;
    # ``news_breakout_buffer_pips`` offsets each stop beyond the range edge;
    # ``news_risk_multiplier`` sizes news windows down (spreads widen) and
    # ``news_post_event_cooldown_s`` is how long after the event unfilled pending
    # orders are left resting before cancellation. The planner reads the profile
    # first and falls back to the EntryConfig defaults when a field is absent.
    news_pre_planning_enabled: bool = False
    news_pre_stage_minutes: int = 5
    news_range_lookback_bars: int = 10
    news_breakout_buffer_pips: float = 5.0
    news_risk_multiplier: float = 0.5
    news_post_event_cooldown_s: float = 300.0


# ---------------------------------------------------------------------------
# Profile definitions per category
# ---------------------------------------------------------------------------

_FOREX_PROFILE = InstrumentProfile(
    category="forex",
    # Structure: forex moves in clean waves — 5-bar lookback is reliable
    swing_lookback=5,
    min_swing_size_pips=3.0,
    # FVG: pips are small, 3-pip proximity catches entries without being too loose
    fvg_proximity_pips=3.0,
    fvg_min_size_pips=2.0,
    # OB: tight buffer, forex respects OBs precisely
    ob_buffer_pips=2.0,
    ob_min_impulse_pips=10.0,
    # Entry: 2-pip SL buffer, minimum 5-pip risk distance
    sl_buffer_pips=2.0,
    min_risk_pips=5.0,
    m1_confirmation_bars=50,
    # Scoring: full confluences available, standard threshold
    min_entry_score=65,
    wyckoff_enabled=True,
    currency_strength_enabled=True,
    news_filter_enabled=True,
    session_score_contribution=True,
    mtf_overlap_threshold_pips=2.0,
)

_COMMODITY_PROFILE = InstrumentProfile(
    category="commodity",
    # Structure: commodities trend strongly but have wide swings
    swing_lookback=6,
    min_swing_size_pips=5.0,
    # FVG: Gold/Oil moves in larger increments — wider proximity
    fvg_proximity_pips=8.0,
    fvg_min_size_pips=4.0,
    # OB: commodities overshoot zones more — wider buffer
    ob_buffer_pips=5.0,
    ob_min_impulse_pips=20.0,
    # Entry: wider SL buffer, rollover spikes make tight stops dangerous
    sl_buffer_pips=5.0,
    min_risk_pips=10.0,
    m1_confirmation_bars=50,
    # Scoring: currency strength irrelevant, Wyckoff valid
    min_entry_score=63,
    wyckoff_enabled=True,
    currency_strength_enabled=False,
    news_filter_enabled=True,
    session_score_contribution=False,   # Gold/Oil trade 24/5, not session-dependent
    mtf_overlap_threshold_pips=5.0,
)

_INDEX_PROFILE = InstrumentProfile(
    category="index",
    # Structure: indices trend in large waves, need wider lookback
    swing_lookback=8,
    min_swing_size_pips=10.0,
    # FVG: US100 trades at 18,000+ points — proximity must be in points not pips
    # pip_size for indices is 0.1, so 15 pips = 1.5 index points
    fvg_proximity_pips=15.0,
    fvg_min_size_pips=8.0,
    # OB: indices regularly overshoot OBs by wide margins before respecting them
    ob_buffer_pips=10.0,
    ob_min_impulse_pips=30.0,
    # Entry: wider SL to survive normal index volatility
    sl_buffer_pips=8.0,
    min_risk_pips=15.0,
    m1_confirmation_bars=50,
    # Scoring: no currency strength, Wyckoff valid (institutional driven)
    min_entry_score=63,
    wyckoff_enabled=True,
    currency_strength_enabled=False,
    news_filter_enabled=True,
    session_score_contribution=False,   # Indices have their own hours, not FX sessions
    mtf_overlap_threshold_pips=8.0,
)

_SYNTHETIC_PROFILE = InstrumentProfile(
    category="synthetic",
    # Structure: synthetics spike randomly — larger lookback avoids false swings
    swing_lookback=10,
    min_swing_size_pips=5.0,
    # FVG: synthetics move fast — wider proximity and bigger minimum gap
    fvg_proximity_pips=15.0,
    fvg_min_size_pips=5.0,
    # OB: synthetics hunt stops very aggressively — wide buffer mandatory
    ob_buffer_pips=10.0,
    ob_min_impulse_pips=20.0,
    # Entry: SL must be meaningful relative to instrument price.
    # Synthetics trade at 100–100,000+ points. A fixed pip minimum is useless.
    # min_risk_pips here acts as a MINIMUM percentage floor:
    # the entry_engine multiplies this by pip_size to get minimum SL distance.
    # For V100 (price ~857, pip=0.01): min_risk_pips=50 → min SL = 0.5 pts (0.06%)
    # This is intentionally wide — synthetics at 400x multiplier need room to breathe.
    # Rule of thumb: SL distance should be >= 0.3% of instrument price.
    sl_buffer_pips=15.0,
    min_risk_pips=50.0,
    m1_confirmation_bars=30,
    # Scoring: no Wyckoff (no real volume), no currency strength, no news
    min_entry_score=58,
    wyckoff_enabled=False,
    currency_strength_enabled=False,
    news_filter_enabled=False,
    session_score_contribution=False,   # 24/7 — sessions irrelevant
    mtf_overlap_threshold_pips=5.0,
)

_CRYPTO_PROFILE = InstrumentProfile(
    category="crypto",
    # Structure: crypto trends strongly but has violent wicks and fast reversals.
    # Larger lookback avoids false swing labels on spike candles.
    swing_lookback=8,
    min_swing_size_pips=10.0,
    # FVG: crypto moves in large dollar increments — proximity must be wide.
    # BTC gaps of $50-200 are common and valid entry zones.
    fvg_proximity_pips=20.0,
    fvg_min_size_pips=8.0,
    # OB: crypto respects order blocks but with wide wicks — buffer essential.
    ob_buffer_pips=12.0,
    ob_min_impulse_pips=25.0,
    # Entry: wide SL buffer — crypto wicks through tight stops constantly.
    # min_risk_pips high because crypto pip values are large ($1+ per pip on BTC).
    sl_buffer_pips=10.0,
    min_risk_pips=20.0,
    m1_confirmation_bars=50,
    # Scoring: Wyckoff valid (real volume), no currency strength, news matters.
    # Lower threshold — currency strength and some session points not available.
    min_entry_score=60,
    wyckoff_enabled=True,               # Crypto follows Wyckoff accumulation/distribution
    currency_strength_enabled=False,    # Crypto vs USD — currency strength not relevant
    news_filter_enabled=True,           # CPI, FOMC, BTC ETF news moves crypto hard
    session_score_contribution=False,   # 24/7 — sessions irrelevant
    mtf_overlap_threshold_pips=10.0,
)

_GOLD_PROFILE = InstrumentProfile(
    category="commodity",
    # Structure: Gold makes clean institutional swings — tight lookback is reliable.
    swing_lookback=5,
    # Gold pip_size is 0.01, so 1 pip = $0.01. All pip fields below are scaled
    # to Gold's real dollar geometry (daily ATR ~$20-40 = 2000-4000 pips), not
    # the single-digit-pip commodity prior which is far too tight for Gold.
    min_swing_size_pips=200.0,          # = $2.00 — minimum meaningful swing on Gold
    # FVG: catch real institutional imbalances, filter spread/noise gaps.
    fvg_proximity_pips=300.0,           # = $3.00 — catches real institutional imbalances
    fvg_min_size_pips=150.0,            # = $1.50 — filters noise gaps
    # OB: Gold overshoots order blocks by $1-3 before respecting them.
    ob_buffer_pips=200.0,               # = $2.00 — Gold overshoots OBs by $1-3
    ob_min_impulse_pips=500.0,          # = $5.00 — real impulsive moves
    # Entry: stops must survive Gold's wicks and clear a sensible minimum.
    sl_buffer_pips=150.0,               # = $1.50 — survive wicks
    min_risk_pips=300.0,                # = $3.00 — minimum sensible Gold stop
    m1_confirmation_bars=50,
    # Scoring: lower threshold because more confluences are available for Gold.
    min_entry_score=60,
    wyckoff_enabled=True,               # Gold follows Wyckoff perfectly
    currency_strength_enabled=True,     # Gold is an anti-USD instrument — DXY matters
    news_filter_enabled=True,           # FOMC/CPI/NFP move Gold $20-50
    session_score_contribution=True,    # Extremely session-driven: London open, NY open
    mtf_overlap_threshold_pips=200.0,   # = $2.00
    # Phase 2: Gold opts in to pre-staged limit orders. staging_proximity_pips
    # is scaled to Gold's dollar geometry ($3.00, matching fvg_proximity_pips) —
    # the generic 5-pip ($0.05) default is meaningless at Gold's scale.
    pre_staging_enabled=True,
    staging_proximity_pips=300.0,       # = $3.00 — Gold-scaled staging proximity
    dxy_filter_enabled=True,            # Gold's inverse-USD correlation is core
    # Phase 3: Gold opts in to news calendar pre-planning. FOMC/CPI/NFP move
    # Gold $20-50 in seconds — staging breakout stops both sides catches the
    # move instead of blocking around it. The buffer is scaled to Gold's dollar
    # geometry ($2.00) — the generic 5-pip ($0.05) default is meaningless here.
    news_pre_planning_enabled=True,
    news_breakout_buffer_pips=200.0,    # = $2.00 — clears widened news spreads
)

# Map category → profile
_PROFILE_MAP: dict[str, InstrumentProfile] = {
    "forex":     _FOREX_PROFILE,
    "commodity": _COMMODITY_PROFILE,
    "index":     _INDEX_PROFILE,
    "synthetic": _SYNTHETIC_PROFILE,
    "crypto":    _CRYPTO_PROFILE,
}

# Symbol-level overrides — consulted by get_profile() BEFORE the category map.
# A symbol listed here gets its dedicated profile instead of its category's
# generic one, letting a single instrument be tuned to its real behaviour.
# Gold's dollar-scale geometry is nothing like Silver/Brent/WTI, so it earns
# its own profile rather than sharing the generic commodity prior.
_SYMBOL_PROFILE_MAP: dict[str, InstrumentProfile] = {
    "XAUUSD": _GOLD_PROFILE,
}


# ---------------------------------------------------------------------------
# Self-calibrating derivation (ATR-normalised geometry)
# ---------------------------------------------------------------------------
#
# The per-category constants above are PRIORS — used at cold start and as a
# floor.  Once a symbol has accrued enough ATR history (``InstrumentStats``),
# its pip-geometry is recomputed as a UNIVERSAL multiple of the symbol's own
# live ATR, so one rule self-calibrates to every instrument instead of a
# category literal.  These ``k`` ratios are the single shared formula set;
# they were anchored against the forex profile at its typical M5 ATR and apply
# unchanged to gold, indices, crypto and synthetics — only the live ATR differs.
_ATR_GEOMETRY_RATIOS: dict[str, float] = {
    "fvg_proximity_pips": 0.60,       # qualify an FVG within ~0.6 ATR of price
    "fvg_min_size_pips": 0.40,        # ignore gaps smaller than ~0.4 ATR (spread noise)
    "ob_buffer_pips": 0.40,           # tolerance around an OB edge
    "ob_min_impulse_pips": 2.00,      # an OB needs a ~2 ATR impulse to qualify
    "sl_buffer_pips": 0.40,           # stop buffer beyond the zone
    "min_risk_pips": 1.00,            # reject setups whose SL is under ~1 ATR
    "min_swing_size_pips": 0.60,      # a swing must clear ~0.6 ATR to count
    "mtf_overlap_threshold_pips": 0.40,
}

# The timeframe whose ATR scales the analysis-plane geometry.
_GEOMETRY_ATR_TF = "M5"

# Reference M5 wick-to-body ratio for a "typical" major-forex instrument.  A
# symbol's measured ratio is divided by this to scale its swing_lookback up
# (wickier than reference) or down (cleaner than reference).
_WICK_BODY_REFERENCE = 2.0

# Optional injected provider: ``symbol -> InstrumentStats | None``.  When unset
# (default), ``get_profile`` returns the category constants unchanged — so the
# self-calibrating path is strictly opt-in and behaviour-preserving until a live
# stats feed is wired and validated.
_stats_provider: Optional[Callable[[str], Any]] = None


def set_stats_provider(provider: Optional[Callable[[str], Any]]) -> None:
    """Register (or clear) the ``symbol -> InstrumentStats`` provider.

    Passing ``None`` reverts ``get_profile`` to the hardcoded category profiles.
    """
    global _stats_provider
    _stats_provider = provider


def derive_profile(base: InstrumentProfile, stats: Any) -> InstrumentProfile:
    """Return a self-calibrated profile: ATR-scaled geometry over ``base``.

    Pip-geometry fields are recomputed as ``k * live_ATR_pips`` (each floored at
    the category constant's lower bound so calibration never makes a threshold
    nonsensically small).  ``swing_lookback`` self-calibrates from the symbol's
    measured M5 wick-to-body ratio (wickier instruments get a wider lookback),
    bounded to ``[3, 15]``.  The remaining structural / enable fields
    (``m1_confirmation_bars``, the ``*_enabled`` flags, ``min_entry_score``)
    keep the category value — those need trade-outcome data, not candle data.
    Falls back to ``base`` unchanged when ``stats`` is missing or not yet
    calibrated, so cold start is identical to today's behaviour.
    """
    try:
        if stats is None or not stats.is_calibrated(_GEOMETRY_ATR_TF):
            return base
        atr_pips = float(stats.atr_pips(_GEOMETRY_ATR_TF))
        if atr_pips <= 0:
            return base
    except Exception:  # noqa: BLE001 — calibration must never break analysis
        return base

    overrides: dict[str, Any] = {}
    for fld, k in _ATR_GEOMETRY_RATIOS.items():
        derived = k * atr_pips
        # Floor at half the category prior so a quiet session can't collapse a
        # threshold to ~0; the prior is the conservative lower bound.
        floor = getattr(base, fld) * 0.5
        overrides[fld] = round(max(derived, floor), 4)

    # Self-calibrate swing_lookback from the symbol's measured wick behaviour.
    # High wick-to-body ratio → violent wicks → needs a wider lookback so spike
    # candles don't get mislabelled as swings.  Low ratio → clean waves → a
    # tighter lookback catches genuine swings earlier.  Falls through to the
    # category default when the stats lack enough samples (wbr is None) or the
    # provider doesn't expose the read.
    try:
        wbr = stats.wick_body_ratio()
        if wbr is not None and wbr > 0:
            scale = max(0.6, min(2.0, wbr / _WICK_BODY_REFERENCE))
            derived_lookback = int(round(base.swing_lookback * scale))
            overrides["swing_lookback"] = max(3, min(15, derived_lookback))
    except Exception:  # noqa: BLE001 — calibration must never break analysis
        pass

    return replace(base, **overrides)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_profile(symbol: str) -> InstrumentProfile:
    """
    Return the InstrumentProfile for a symbol.

    Looks up a symbol-level override in ``_SYMBOL_PROFILE_MAP`` first, then falls
    back to the category profile from INSTRUMENT_REGISTRY for the base
    (cold-start) profile.  When a stats provider is registered and the symbol is
    calibrated, returns an ATR-normalised, self-calibrated profile derived from
    that symbol's own live volatility.  Falls back to forex if the symbol is
    unknown, and to the category constants whenever stats are unavailable.
    """
    key = symbol.upper()
    override = _SYMBOL_PROFILE_MAP.get(key)
    if override is not None:
        base = override
    else:
        info = INSTRUMENT_REGISTRY.get(key)
        if info is None:
            logger.warning(
                "InstrumentProfile — unknown symbol '{}', using forex defaults", symbol
            )
            base = _FOREX_PROFILE
        else:
            category = info.category.value   # "forex" / "commodity" / "index" / "synthetic"
            base = _PROFILE_MAP.get(category, _FOREX_PROFILE)

    profile = base
    if _stats_provider is not None:
        try:
            stats = _stats_provider(symbol.upper())
            profile = derive_profile(base, stats)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "InstrumentProfile — stats derivation failed for {}: {}", symbol, exc
            )
            profile = base

    logger.debug(
        "InstrumentProfile — {} → category={} | swing_lookback={} | "
        "fvg_proximity={} | sl_buffer={} | min_score={} | calibrated={}",
        symbol, profile.category,
        profile.swing_lookback,
        profile.fvg_proximity_pips,
        profile.sl_buffer_pips,
        profile.min_entry_score,
        profile is not base,
    )
    return profile

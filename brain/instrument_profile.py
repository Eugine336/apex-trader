"""
APEX TRADER — Instrument Profile
The single source of truth for per-instrument analysis parameters.

Every detector, engine, and scanner pulls its tuning values from here
instead of using hardcoded numbers. Add a new symbol to config.py,
assign it a category — it immediately gets the right profile.

No symbol names are hardcoded here. Everything is driven by category
so the system scales to any number of instruments automatically.
"""

from dataclasses import dataclass
from loguru import logger

from config import INSTRUMENT_REGISTRY, InstrumentCategory


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
    # FVG: synthetics move fast and leave larger gaps
    fvg_proximity_pips=8.0,
    fvg_min_size_pips=3.0,
    # OB: synthetics hunt stops aggressively — wider buffer essential
    ob_buffer_pips=6.0,
    ob_min_impulse_pips=15.0,
    # Entry: wide SL buffer, synthetics will wick through tight stops
    sl_buffer_pips=6.0,
    min_risk_pips=8.0,
    m1_confirmation_bars=30,    # Synthetic M1 bars are faster — less history needed
    # Scoring: no Wyckoff (no real volume), no currency strength, no news
    # Lower threshold because fewer confluences are achievable
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

# Map category → profile
_PROFILE_MAP: dict[str, InstrumentProfile] = {
    "forex":     _FOREX_PROFILE,
    "commodity": _COMMODITY_PROFILE,
    "index":     _INDEX_PROFILE,
    "synthetic": _SYNTHETIC_PROFILE,
    "crypto":    _CRYPTO_PROFILE,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_profile(symbol: str) -> InstrumentProfile:
    """
    Return the InstrumentProfile for a symbol.
    Looks up the category from INSTRUMENT_REGISTRY and returns
    the matching profile. Falls back to forex if unknown.
    """
    info = INSTRUMENT_REGISTRY.get(symbol.upper())
    if info is None:
        logger.warning(
            "InstrumentProfile — unknown symbol '{}', using forex defaults", symbol
        )
        return _FOREX_PROFILE

    category = info.category.value   # "forex" / "commodity" / "index" / "synthetic"
    profile = _PROFILE_MAP.get(category, _FOREX_PROFILE)

    logger.debug(
        "InstrumentProfile — {} → category={} | swing_lookback={} | "
        "fvg_proximity={} | sl_buffer={} | min_score={}",
        symbol, category,
        profile.swing_lookback,
        profile.fvg_proximity_pips,
        profile.sl_buffer_pips,
        profile.min_entry_score,
    )
    return profile

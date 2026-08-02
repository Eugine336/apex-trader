"""Tests for VWAP-as-zone extraction (entry/zone_watcher.py + brain/session_vwap.py).

Covers the VWAP deviation-band entry zones synthesized by
:func:`entry.zone_watcher.extract_entry_zones` when ``vwap_zone_enabled`` is set:

  * A LONG zone when price is near the lower band in an uptrend.
  * A SHORT zone when price is near the upper band in a downtrend.
  * No zone when price sits between the bands (not near either edge).
  * No zone when the feature is disabled via the InstrumentProfile.
  * VWAP_BAND zones flow through the ZoneWatcher pipeline.
  * Invalidation is placed beyond the band edge.
  * Conviction is the configured profile value.

Deterministic: hand-built M5 frames and structure; no network, disk, or
wall-clock-dependent assertions.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd

from brain.instrument_profile import get_profile
from brain.session_vwap import vwap_with_bands
from brain.structure_engine import StructureAnalysis, StructureEvent, Trend
from brain.world_model import WorldModelStore, build_world_model
from entry.models import EntryConfig, ZoneType
from entry.zone_watcher import ZoneWatcher, extract_entry_zones


# ── Fixtures / builders ────────────────────────────────────────────────────
# A price series with genuine dispersion so the deviation bands are non-empty.
_CLOSES = [100.0, 101.0, 99.0, 100.5, 99.5, 100.0, 101.0, 99.0, 100.5, 99.5, 100.0, 100.0]
_SESSION_MINUTES = len(_CLOSES) * 5  # 60 min → all 12 M5 bars anchor the VWAP


def _m5_df(closes=None, vols=None) -> pd.DataFrame:
    closes = list(closes if closes is not None else _CLOSES)
    n = len(closes)
    vols = list(vols) if vols is not None else [100.0] * n
    base = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    times = [base + timedelta(minutes=5 * i) for i in range(n)]
    # high == low == close keeps the typical price equal to the close, so the
    # VWAP + std are fully determined by the close series.
    return pd.DataFrame({
        "time": times,
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "tick_volume": vols,
    })


def _structure(trend: Trend) -> StructureAnalysis:
    return StructureAnalysis(
        trend=trend, last_event=StructureEvent.BOS_BULLISH,
        swing_high=101.0, swing_low=99.0,
        last_bos_level=100.5, last_choch_level=None,
        structure_broken=False,
        bullish_swing_points=[], bearish_swing_points=[],
        confidence=0.8,
    )


def _model(store, trend: Trend):
    return build_world_model(
        symbol="XAUUSD",
        version=store.next_version(),
        structure={"H4": _structure(trend)},
    )


def _bands(df=None, num_std=1.5):
    df = df if df is not None else _m5_df()
    bands = vwap_with_bands(df, _SESSION_MINUTES, num_std=num_std)
    assert bands is not None
    vwap, upper, lower = bands
    # Sanity: dispersion is real, so the bands straddle the VWAP.
    assert lower < vwap < upper
    return vwap, upper, lower


def _vwap_zones(zones):
    return [z for z in zones if z.zone_type == ZoneType.VWAP_BAND]


# ── vwap_with_bands ────────────────────────────────────────────────────────
def test_vwap_with_bands_symmetric_around_vwap():
    vwap, upper, lower = _bands()
    assert abs((upper - vwap) - (vwap - lower)) < 1e-9


def test_vwap_with_bands_none_when_insufficient_bars():
    assert vwap_with_bands(_m5_df(closes=[100.0, 100.0]), 10) is None


# ── Zone generation ────────────────────────────────────────────────────────
def test_long_zone_when_price_near_lower_band_uptrend():
    store = WorldModelStore()
    df = _m5_df()
    _vwap, _upper, lower = _bands(df)
    zones = extract_entry_zones(
        _model(store, Trend.BULLISH),
        EntryConfig(),
        m5_df=df,
        session_open_minutes=_SESSION_MINUTES,
        current_price=lower,
        profile=get_profile("XAUUSD"),
    )
    vwap_zones = _vwap_zones(zones)
    assert len(vwap_zones) == 1
    z = vwap_zones[0]
    assert z.direction == "LONG"
    assert abs(z.midpoint - lower) < 1e-9


def test_short_zone_when_price_near_upper_band_downtrend():
    store = WorldModelStore()
    df = _m5_df()
    _vwap, upper, _lower = _bands(df)
    zones = extract_entry_zones(
        _model(store, Trend.BEARISH),
        EntryConfig(),
        m5_df=df,
        session_open_minutes=_SESSION_MINUTES,
        current_price=upper,
        profile=get_profile("XAUUSD"),
    )
    vwap_zones = _vwap_zones(zones)
    assert len(vwap_zones) == 1
    z = vwap_zones[0]
    assert z.direction == "SHORT"
    assert abs(z.midpoint - upper) < 1e-9


def test_no_zone_when_price_between_bands():
    store = WorldModelStore()
    df = _m5_df()
    vwap, _upper, _lower = _bands(df)
    # Price parked at the VWAP is a full deviation multiple from either edge.
    zones = extract_entry_zones(
        _model(store, Trend.BULLISH),
        EntryConfig(),
        m5_df=df,
        session_open_minutes=_SESSION_MINUTES,
        current_price=vwap,
        profile=get_profile("XAUUSD"),
    )
    assert _vwap_zones(zones) == []


def test_no_zone_when_disabled_via_profile():
    store = WorldModelStore()
    df = _m5_df()
    _vwap, _upper, lower = _bands(df)
    disabled = replace(get_profile("XAUUSD"), vwap_zone_enabled=False)
    zones = extract_entry_zones(
        _model(store, Trend.BULLISH),
        EntryConfig(),
        m5_df=df,
        session_open_minutes=_SESSION_MINUTES,
        current_price=lower,
        profile=disabled,
    )
    assert _vwap_zones(zones) == []


def test_vwap_band_zone_flows_through_zone_watcher():
    store = WorldModelStore()
    watcher = ZoneWatcher(store)
    df = _m5_df()
    _vwap, _upper, lower = _bands(df)
    model = _model(store, Trend.BULLISH)
    zones = extract_entry_zones(
        model,
        EntryConfig(),
        m5_df=df,
        session_open_minutes=_SESSION_MINUTES,
        current_price=lower,
        profile=get_profile("XAUUSD"),
    )
    store.publish(replace(model, entry_zones=tuple(zones)))
    watcher.on_world_model_update("XAUUSD")
    active = watcher.get_active_zones("XAUUSD")
    assert any(z.zone_type == ZoneType.VWAP_BAND for z in active)


def test_invalidation_is_beyond_the_band_edge():
    store = WorldModelStore()
    df = _m5_df()
    _vwap, upper, lower = _bands(df)

    long_zones = _vwap_zones(extract_entry_zones(
        _model(store, Trend.BULLISH), EntryConfig(),
        m5_df=df, session_open_minutes=_SESSION_MINUTES,
        current_price=lower, profile=get_profile("XAUUSD"),
    ))
    assert long_zones[0].invalidation_level < lower

    short_zones = _vwap_zones(extract_entry_zones(
        _model(store, Trend.BEARISH), EntryConfig(),
        m5_df=df, session_open_minutes=_SESSION_MINUTES,
        current_price=upper, profile=get_profile("XAUUSD"),
    ))
    assert short_zones[0].invalidation_level > upper


def test_conviction_is_the_configured_value():
    store = WorldModelStore()
    df = _m5_df()
    _vwap, _upper, lower = _bands(df)
    profile = replace(get_profile("XAUUSD"), vwap_zone_conviction=72)
    zones = _vwap_zones(extract_entry_zones(
        _model(store, Trend.BULLISH), EntryConfig(),
        m5_df=df, session_open_minutes=_SESSION_MINUTES,
        current_price=lower, profile=profile,
    ))
    assert zones[0].conviction == 72

    # Default profile conviction (65) when not overridden.
    default_zones = _vwap_zones(extract_entry_zones(
        _model(store, Trend.BULLISH), EntryConfig(),
        m5_df=df, session_open_minutes=_SESSION_MINUTES,
        current_price=lower, profile=get_profile("XAUUSD"),
    ))
    assert default_zones[0].conviction == 65

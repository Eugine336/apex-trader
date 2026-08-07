"""Tests for the VWAP-as-zone source (entry.zone_watcher + brain.session_vwap).

Covers:
  * A LONG VWAP_BAND zone when price dips to the lower band in an uptrend.
  * A SHORT VWAP_BAND zone when price rallies to the upper band in a downtrend.
  * No zone when price sits between the bands (near neither edge).
  * No zone when the feature is disabled on the profile.
  * VWAP_BAND zones flow through the extraction → WorldModel → ZoneWatcher path
    alongside FVG/OB zones.
  * The invalidation level sits beyond the touched band edge.
  * The zone conviction is the configured profile value.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd

from brain.fvg_detector import FairValueGap, FVGStatus
from brain.instrument_profile import get_profile
from brain.session_vwap import vwap_with_bands
from brain.structure_engine import StructureAnalysis, StructureEvent, Trend
from brain.world_model import WorldModelStore, build_world_model
from entry.models import EntryConfig, ZoneType
from entry.zone_watcher import ZoneWatcher, extract_entry_zones


SYMBOL = "EURUSD"


def _ts(offset_minutes: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)


def _m5_df(closes: list[float]) -> pd.DataFrame:
    """Build an M5 DataFrame (high=low=close) with flat unit volume and a
    monotonically increasing time column so session minutes can be derived."""
    n = len(closes)
    times = [
        datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=5 * i)
        for i in range(n)
    ]
    return pd.DataFrame(
        {
            "time": times,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "tick_volume": [100.0] * n,
        }
    )


def _session_minutes(df: pd.DataFrame) -> int:
    return len(df) * 5


def _structure(trend: Trend) -> StructureAnalysis:
    return StructureAnalysis(
        trend=trend, last_event=StructureEvent.BOS_BULLISH,
        swing_high=1.0950, swing_low=1.0750,
        last_bos_level=1.0900, last_choch_level=None,
        structure_broken=False,
        bullish_swing_points=[], bearish_swing_points=[],
        confidence=0.8,
    )


def _make_fvg() -> FairValueGap:
    return FairValueGap(
        kind="BULLISH", top=1.0860, bottom=1.0855, midpoint=1.08575,
        size_pips=5.0, strength="STRONG", status=FVGStatus.OPEN,
        candle_index=50, timestamp=_ts(), timeframe="M5",
    )


def _model(trend: Trend, fvgs=None):
    store = WorldModelStore()
    return build_world_model(
        symbol=SYMBOL,
        version=store.next_version(),
        fvgs=fvgs,
        structure={"H4": _structure(trend)},
    )


def _dip_df() -> pd.DataFrame:
    # 20 bars clustered around 1.0850, final bar a strong dip well below the
    # lower band → price near/through the lower VWAP band.
    return _m5_df([1.0850] * 20 + [1.0700])


def _rally_df() -> pd.DataFrame:
    # 20 bars clustered around 1.0850, final bar a strong rally well above the
    # upper band → price near/through the upper VWAP band.
    return _m5_df([1.0850] * 20 + [1.1000])


def _mid_df() -> pd.DataFrame:
    # Spread cluster (variance > 0) whose final close sits at the VWAP centre —
    # near neither band.
    closes = [1.0830, 1.0870] * 10 + [1.0850]
    return _m5_df(closes)


def _extract(df, trend, *, profile=None, config=None, fvgs=None):
    model = _model(trend, fvgs=fvgs)
    prof = profile if profile is not None else get_profile(SYMBOL)
    cfg = config or EntryConfig()
    return extract_entry_zones(
        model, cfg,
        m5_df=df,
        session_open_minutes=_session_minutes(df),
        profile=prof,
    )


class TestVwapZoneGeneration:
    def test_long_zone_when_price_near_lower_band_in_uptrend(self):
        df = _dip_df()
        vwap, upper, lower = vwap_with_bands(df, _session_minutes(df), num_std=1.5)
        zones = [z for z in _extract(df, Trend.BULLISH) if z.zone_type == ZoneType.VWAP_BAND]
        assert len(zones) == 1
        z = zones[0]
        assert z.direction == "LONG"
        assert z.midpoint == lower
        assert z.top > z.bottom

    def test_short_zone_when_price_near_upper_band_in_downtrend(self):
        df = _rally_df()
        vwap, upper, lower = vwap_with_bands(df, _session_minutes(df), num_std=1.5)
        zones = [z for z in _extract(df, Trend.BEARISH) if z.zone_type == ZoneType.VWAP_BAND]
        assert len(zones) == 1
        z = zones[0]
        assert z.direction == "SHORT"
        assert z.midpoint == upper
        assert z.top > z.bottom

    def test_no_zone_when_price_between_bands(self):
        df = _mid_df()
        # A band must exist (variance > 0) so the absence of a zone is due to the
        # proximity check, not a degenerate session.
        assert vwap_with_bands(df, _session_minutes(df), num_std=1.5) is not None
        zones = [z for z in _extract(df, Trend.BULLISH) if z.zone_type == ZoneType.VWAP_BAND]
        assert zones == []

    def test_no_zone_when_disabled_via_profile(self):
        df = _dip_df()
        disabled = replace(get_profile(SYMBOL), vwap_zone_enabled=False)
        zones = [
            z for z in _extract(df, Trend.BULLISH, profile=disabled)
            if z.zone_type == ZoneType.VWAP_BAND
        ]
        assert zones == []

    def test_no_zone_without_htf_bias(self):
        df = _dip_df()
        zones = [z for z in _extract(df, Trend.RANGING) if z.zone_type == ZoneType.VWAP_BAND]
        assert zones == []


class TestVwapZonePipeline:
    def test_vwap_zone_flows_through_pipeline_with_fvg(self):
        df = _dip_df()
        zones = _extract(df, Trend.BULLISH, fvgs={"M5": [_make_fvg()]})
        types = {z.zone_type for z in zones}
        assert ZoneType.VWAP_BAND in types
        assert ZoneType.FVG_MIDPOINT in types

        # The synthesized zones are what the analysis plane stores on the
        # WorldModel; the ZoneWatcher must surface the VWAP zone unchanged.
        store = WorldModelStore()
        wm = build_world_model(
            symbol=SYMBOL, version=store.next_version(), entry_zones=zones,
        )
        store.publish(wm)
        watcher = ZoneWatcher(store)
        watcher.on_world_model_update(SYMBOL)
        active = watcher.get_active_zones(SYMBOL)
        assert any(z.zone_type == ZoneType.VWAP_BAND for z in active)


class TestVwapZoneGeometry:
    def test_invalidation_beyond_lower_band_for_long(self):
        df = _dip_df()
        _vwap, _upper, lower = vwap_with_bands(df, _session_minutes(df), num_std=1.5)
        z = [z for z in _extract(df, Trend.BULLISH) if z.zone_type == ZoneType.VWAP_BAND][0]
        assert z.invalidation_level < lower

    def test_invalidation_beyond_upper_band_for_short(self):
        df = _rally_df()
        _vwap, upper, _lower = vwap_with_bands(df, _session_minutes(df), num_std=1.5)
        z = [z for z in _extract(df, Trend.BEARISH) if z.zone_type == ZoneType.VWAP_BAND][0]
        assert z.invalidation_level > upper

    def test_conviction_is_configured_value(self):
        df = _dip_df()
        prof = replace(get_profile(SYMBOL), vwap_zone_conviction=65)
        z = [
            z for z in _extract(df, Trend.BULLISH, profile=prof)
            if z.zone_type == ZoneType.VWAP_BAND
        ][0]
        assert z.conviction == 65

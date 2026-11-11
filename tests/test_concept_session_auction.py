"""Deterministic tests for session, VWAP, volume-profile and auction readings."""
from __future__ import annotations

import time

import pandas as pd
import pytest

from cognition.brain_reasoning import BrainReasoner
from cognition.concepts.auction_utils import clean_bars
from cognition.concepts.opening_range import OpeningRange
from cognition.concepts.range_rotation import RangeRotation
from cognition.concepts.seasonality import SeasonalityConcept
from cognition.concepts.session_calendar import SessionCalendar
from cognition.concepts.volume_profile import VolumeProfileConcept
from cognition.concepts.vwap import VWAPConcept
from cognition.concepts.base import reading_to_evidence
from cognition.contracts import MarketState
from cognition.market_lenses import analyze_market_frames
from cognition.opportunity_geometry import derive_opportunity_geometry


def _bar(epoch, values, volume=100.0, session_open=None):
    opening, high, low, close = values
    bar = {
        "open": opening, "high": high, "low": low, "close": close,
        "volume": volume, "volume_source": "provider_volume",
        "timestamp_epoch": float(epoch), "source_timestamp_verified": True,
        "tick_size": 0.5, "tick_size_verified": True,
    }
    if session_open is not None:
        bar.update({
            "session_id": "NYSE", "session_open_epoch": float(session_open),
            "session_close_epoch": float(session_open + 8 * 3600),
            "session_timezone": "America/New_York", "session_calendar_verified": True,
            "dst_status": "standard", "holiday_status": "regular",
            "rollover_window": "last_half_hour",
            "rollover_start_epoch": float(session_open + 7 * 3600),
            "rollover_end_epoch": float(session_open + 7 * 3600 + 1800),
            "liquidity": "normal", "is_tradeable": True,
            "minutes_to_next_session": 24 * 60,
        })
    return bar


def _session_bars(base=1_700_000_000):
    previous_open = base - 24 * 3600
    previous = [
        (100, 102, 98, 101), (101, 104, 100, 103), (103, 103, 99, 100),
        (100, 102, 97, 98), (98, 101, 96, 100), (100, 103, 98, 102),
        (102, 112, 101, 110), (110, 111, 106, 108),
    ]
    current = [
        (100, 102, 98, 101), (101, 105, 100, 104), (104, 103, 97, 99),
        (99, 101, 95, 96), (96, 102, 97, 101), (101, 104, 98, 103),
        (103, 107, 102, 106), (106, 108, 104.5, 107), (107, 108, 105, 106.5),
    ]
    bars = [
        _bar(previous_open + index * 300, row, session_open=previous_open)
        for index, row in enumerate(previous)
    ]
    bars.extend(
        _bar(base + index * 300, row, session_open=base)
        for index, row in enumerate(current)
    )
    return bars


def _range_bars(base=1_700_000_000):
    rows = [
        (100, 101, 99, 100),
        (101, 102, 100, 101),   # confirmed swing high
        (101, 101, 99, 100),
        (100, 100.5, 98, 99),   # confirmed swing low
        (99, 101, 99, 100),
        (100, 102, 100, 101),
        (101, 105, 101, 104),   # newer confirmed swing high
        (104, 103, 99, 100),
        (100, 102, 95, 96),     # newer confirmed swing low
        (96, 104, 97, 101),
        (101, 102, 98, 100),
        (100, 102, 96, 99),
        (99, 101, 94, 97),      # failed downside auction
    ]
    return [
        _bar(base + index * 300, row, volume=500.0 if index == 6 else 100.0)
        for index, row in enumerate(rows)
    ]


def _profile_bars(base=1_700_000_000):
    rows = [
        ((100, 101, 99, 100), 50),
        ((100, 101.5, 99.5, 101), 50),
        ((101, 102, 100, 101.5), 50),
        ((100, 102, 98, 100), 500),       # unique POC node
        ((101, 102, 100, 101), 50),
        ((100, 101, 99, 100), 50),
        ((100, 101, 99, 100), 50),
        ((109, 111, 108, 110), 1),       # completed acceptance above value
    ]
    return [_bar(base + i * 300, row, volume=volume) for i, (row, volume) in enumerate(rows)]


def _seasonality_bars(base=1_700_000_000):
    rows = []
    for index in range(20):
        opening = 100.0 + index
        close = opening + 1.0
        rows.append(_bar(base + index * 86400, (opening, close + 0.2, opening - 0.2, close)))
    return rows


def _mirror(bars, constant=200.0):
    mirrored = []
    for bar in bars:
        item = dict(bar)
        item["open"] = constant - bar["open"]
        item["high"] = constant - bar["low"]
        item["low"] = constant - bar["high"]
        item["close"] = constant - bar["close"]
        mirrored.append(item)
    return mirrored


def _reading(module, bars, timeframe="M5"):
    readings = module.read("EURUSD", {timeframe: bars})
    assert readings, module.key
    return readings[0]


def _implications(reading):
    return {item.direction: item for item in reading.implications}


def _assert_mirror(module, bars, timeframe="M5"):
    original = _reading(module, bars, timeframe)
    mirrored = _reading(module, _mirror(bars), timeframe)
    left, right = _implications(original), _implications(mirrored)
    for direction, opposite in (("LONG", "SHORT"), ("SHORT", "LONG")):
        first, second = left[direction], right[opposite]
        assert first.activated is second.activated, (module.key, direction, original.facts, mirrored.facts)
        if first.strength is not None and second.strength is not None:
            assert second.strength == pytest.approx(first.strength)
        for field in ("invalidation_price", "objective_price"):
            value = getattr(first, field)
            mirrored_value = getattr(second, field)
            if value is not None:
                assert mirrored_value == pytest.approx(200.0 - value), (module.key, field, value, mirrored_value)
        if first.milestones:
            assert tuple(200.0 - value for value in first.milestones) == pytest.approx(second.milestones)
    return original, mirrored


def test_session_calendar_keeps_unverified_session_state_unknown():
    bars = _range_bars()
    reading = _reading(SessionCalendar(), bars)
    assert reading.facts["calendar_verified"] is False
    assert reading.facts["status"] == "unverified_or_missing"
    assert any("authoritative venue session calendar" in item for item in reading.unknowns)
    assert reading.implications[0].direction == "FLAT"
    assert reading.implications[0].activated is False


def test_session_calendar_reads_verified_dst_holiday_and_rollover_facts():
    reading = _reading(SessionCalendar(), _session_bars())
    assert reading.facts["calendar_verified"] is True
    assert reading.facts["status"] == "in_session"
    assert reading.facts["dst_status"] == "standard"
    assert reading.facts["holiday_status"] == "regular"
    assert reading.facts["rollover_active"] is False
    assert reading.facts["minutes_to_close"] is not None
    assert not reading.unknowns


def test_opening_range_requires_verified_session_and_reads_acceptance_retest():
    reading = _reading(OpeningRange(), _session_bars())
    assert reading.facts["opening_range_high"] == pytest.approx(105)
    assert reading.facts["opening_range_low"] == pytest.approx(95)
    assert reading.facts["auction_state"] == "accepted_above_retested"
    assert reading.facts["historical_opening_range_samples"] == 1
    long = _implications(reading)["LONG"]
    assert long.activated is True
    assert long.invalidation_price == pytest.approx(105)
    assert long.objective_price == pytest.approx(112)
    assert long.strength is not None


def test_opening_range_does_not_infer_static_session_windows():
    reading = _reading(OpeningRange(), _range_bars())
    assert reading.facts["status"] == "data_gated"
    assert reading.implications[0].objective_price is None
    assert reading.strength if hasattr(reading, "strength") else reading.implications[0].strength is None
    assert any("authoritative venue session calendar" in item for item in reading.unknowns)


def test_vwap_reads_session_and_anchored_values_and_preserves_unknowns():
    reading = _reading(VWAPConcept(), _session_bars())
    assert reading.facts["session_vwap"] is not None
    assert reading.facts["anchored_vwap"] is not None
    assert reading.facts["primary_vwap"] is not None
    assert reading.facts["anchor_kind"] in {"high", "low"}
    assert reading.facts["absolute_deviation_percentile"] is not None
    assert _implications(reading)["LONG"].activated is True


def test_volume_profile_builds_weekly_value_and_discloses_missing_vap():
    reading = _reading(VolumeProfileConcept(), _profile_bars())
    assert reading.facts["current_week_profile"] is not None
    assert reading.facts["poc"] == pytest.approx(100)
    assert reading.facts["vah"] >= reading.facts["poc"]
    assert reading.facts["val"] <= reading.facts["poc"]
    assert reading.facts["status"] == "accepted_above_value"
    assert _implications(reading)["LONG"].activated is True
    assert any("true trade-level volume-at-price" in item for item in reading.unknowns)


def test_seasonality_uses_prior_same_bucket_samples_and_never_triggers():
    reading = _reading(SeasonalityConcept(), _seasonality_bars(), "D1")
    effects = reading.facts["effects"]
    assert effects["time_of_day"]["sample_count"] > 0
    assert effects["month"]["sample_count"] > 0
    assert reading.facts["long_effect_log_return"] > 0
    assert _implications(reading)["LONG"].activated is False
    assert _implications(reading)["LONG"].objective_price is None
    assert any("instrument-local timezone" in item for item in reading.unknowns)


def test_range_rotation_reads_failed_low_with_structural_geometry():
    reading = _reading(RangeRotation(), _range_bars())
    long = _implications(reading)["LONG"]
    assert reading.facts["status"] == "failed_low_auction"
    assert reading.facts["range_high"] == pytest.approx(105)
    assert reading.facts["range_low"] == pytest.approx(95)
    assert long.activated is True
    assert long.invalidation_price == pytest.approx(95)
    assert long.objective_price is not None and long.objective_price > 97
    assert long.strength == pytest.approx(1.0)
    assert long.milestones == pytest.approx((100.0,))


def test_price_mirror_symmetry_for_all_directional_wp_a_readers():
    _assert_mirror(OpeningRange(), _session_bars())
    _assert_mirror(VWAPConcept(), _session_bars())
    _assert_mirror(VolumeProfileConcept(), _profile_bars())
    _assert_mirror(RangeRotation(), _range_bars())


def test_session_calendar_is_price_mirror_invariant():
    original = _reading(SessionCalendar(), _session_bars())
    mirrored = _reading(SessionCalendar(), _mirror(_session_bars()))
    assert original.facts["status"] == mirrored.facts["status"]
    assert original.facts["session_id"] == mirrored.facts["session_id"]
    assert original.facts["session_open_epoch"] == mirrored.facts["session_open_epoch"]


def test_every_wp_a_reading_converts_to_non_directional_evidence():
    samples = (
        (SessionCalendar(), _session_bars(), "M5"),
        (OpeningRange(), _session_bars(), "M5"),
        (VWAPConcept(), _session_bars(), "M5"),
        (VolumeProfileConcept(), _profile_bars(), "M5"),
        (SeasonalityConcept(), _seasonality_bars(), "D1"),
        (RangeRotation(), _range_bars(), "M5"),
    )
    for module, bars, timeframe in samples:
        for reading in module.read("EURUSD", {timeframe: bars}):
            evidence = reading_to_evidence("EURUSD", reading)
            assert evidence.polarity == 0.0
            assert evidence.measurements["source_kind"] == "concept_reading"


def test_range_rotation_flows_from_candles_to_hypothesis_and_geometry(monkeypatch):
    import cognition.concepts as concepts

    monkeypatch.setattr(concepts, "CONCEPT_MODULES", (RangeRotation(),))
    rows = _range_bars(base=int(time.time() // 300) * 300 - 13 * 300)
    forming_epoch = int(time.time() // 300) * 300
    frame_rows = [
        [bar["timestamp_epoch"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]]
        for bar in rows
    ]
    frame_rows.append([forming_epoch, 100, 150, 50, 120, 1])
    frame = pd.DataFrame(frame_rows, columns=["time", "open", "high", "low", "close", "volume"])
    frame.attrs.update({
        "source_timestamp_verified": True, "source_symbol": "EURUSD",
        "source_timestamp_unit": "unix_seconds", "source_timestamp_origin": "broker_ohlcv",
        "source_broker": "mt5", "tick_size": 0.5, "tick_size_verified": True,
    })
    state = MarketState(symbol="EURUSD")
    evidence = analyze_market_frames("EURUSD", {"M5": frame})
    for item in evidence:
        state.add(item)
    concept_evidence = [
        item for item in state.evidence
        if item.measurements.get("source_kind") == "concept_reading"
        and item.measurements.get("concept") == "range_rotation"
    ]
    assert len(concept_evidence) == 1
    assert concept_evidence[0].polarity == 0.0
    assert concept_evidence[0].source_timestamp_epoch == rows[-1]["timestamp_epoch"]

    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state)
    hypotheses = reasoner.generate_hypotheses(analysis, "EURUSD")
    assert any(
        item.hypothesis_kind == "concept_group:m5:long:active"
        and "range_rotation" in item.supporting_patterns
        for item in hypotheses
    )
    geometry = derive_opportunity_geometry(state, "LONG")
    assert geometry is not None and geometry.concept == "range_rotation"
    assert geometry.stop_price == pytest.approx(95)
    assert geometry.target_price > geometry.entry_price


def test_session_calendar_surfaces_broker_weekly_schedule_without_timezone_inference():
    module = SessionCalendar()
    readings = module.read_with_context(
        "XAUUSD", {"M5": _range_bars()},
        {"symbol_spec": {
            "source": "mt5.symbol_info",
            "source_verified": True,
            "specification_verified": True,
            "broker_trading_sessions": [{
                "day": "monday", "start_seconds_server_time": 3600,
                "end_seconds_server_time": 82800,
            }],
            "broker_session_timezone_verified": False,
            "broker_holiday_calendar_verified": False,
        }},
    )
    reading = readings[0]
    assert reading.facts["broker_weekly_schedule_available"] is True
    assert reading.facts["calendar_verified"] is False
    assert reading.facts["broker_session_timezone_verified"] is False
    assert any("timezone mapping and DST" in item for item in reading.unknowns)


def test_session_calendar_consumes_verified_provider_timezone_and_holiday_observation():
    from data_sources import (
        Completeness, DataObservation, ObservationKind,
        ObservationProvenance, SourceIdentity,
    )

    class _Registry:
        def fetch(self, kind, *, canonical_instrument=None, **kwargs):
            if kind is not ObservationKind.SESSION or canonical_instrument != "XAUUSD":
                return ()
            return (DataObservation(
                provenance=ObservationProvenance(
                    source=SourceIdentity("calendar", "venue", "XAUUSD", "GOLD"),
                    kind=ObservationKind.SESSION,
                    source_timestamp_epoch=1_800_000_000,
                    received_timestamp_epoch=1_800_000_010,
                    source_timestamp_verified=True,
                    completeness=Completeness.COMPLETE,
                    source_record_id="session-1",
                ),
                values={
                    "timezone": "Europe/London",
                    "session_id": "gold-session-1",
                    "session_open_epoch": 1_799_999_000,
                    "session_close_epoch": 1_800_100_000,
                    "dst_status": True,
                    "holiday_status": False,
                    "rollover_start_epoch": 1_800_010_000,
                    "rollover_end_epoch": 1_800_020_000,
                    "rollover_window": "daily_rollover",
                    "liquidity": "regular",
                    "is_tradeable": True,
                },
            ),)

    reading = SessionCalendar().read_with_context(
        "XAUUSD", {"M5": _range_bars(base=1_799_996_400)},
        {"data_source_registry": _Registry(),
         "analysis_epoch": 1_800_000_020},
    )[0]
    assert reading.facts["provider_session_calendar_verified"] is True
    assert reading.facts["calendar_verified"] is True
    assert reading.facts["status"] == "in_session"
    assert reading.facts["session_id"] == "gold-session-1"
    assert reading.facts["session_open_epoch"] == 1_799_999_000
    assert reading.facts["rollover_active"] is False
    assert reading.facts["provider_session_timezone"] == "Europe/London"
    assert reading.facts["provider_dst_status"] is True
    assert reading.facts["provider_holiday_status"] is False
    assert reading.facts["provider_is_tradeable"] is True


def test_session_calendar_rejects_provider_calendar_after_analysis_cutoff():
    from data_sources import (
        Completeness, DataObservation, ObservationKind,
        ObservationProvenance, SourceIdentity,
    )

    class _Registry:
        def fetch(self, kind, **kwargs):
            return (DataObservation(
                provenance=ObservationProvenance(
                    source=SourceIdentity("calendar", "venue", "XAUUSD", "GOLD"),
                    kind=ObservationKind.SESSION,
                    source_timestamp_epoch=1_800_000_100,
                    received_timestamp_epoch=1_800_000_110,
                    source_timestamp_verified=True,
                    completeness=Completeness.COMPLETE,
                    source_record_id="future-session",
                ),
                values={"timezone": "Europe/London",
                        "session_open_epoch": 1_800_000_000,
                        "session_close_epoch": 1_800_100_000},
            ),)

    reading = SessionCalendar().read_with_context(
        "XAUUSD", {"M5": _range_bars()},
        {"data_source_registry": _Registry(), "analysis_epoch": 1_800_000_050},
    )[0]
    assert reading.facts["provider_session_calendar_verified"] is False

"""External-feed prerequisites remain explicit rather than inferred from OHLC."""
from cognition.concepts.external_data_gated_readings import (
    DATA_GATED_METHODS,
    ExternalDataGatedReadings,
)
from cognition.concepts import CONCEPT_MODULES


def test_all_remaining_methods_have_named_external_data_gates():
    expected = {
        "smt_divergence", "cross_asset", "currency_strength", "relative_value",
        "event_driven", "macro_rates", "macro_growth", "carry", "derivatives",
        "crypto_microstructure", "synthetic_market", "delta_cvd", "footprint",
        "absorption_exhaustion", "market_impact",
    }
    assert set(DATA_GATED_METHODS) == expected
    module = next(item for item in CONCEPT_MODULES if item.key == "external_data_gated_readings")
    assert set(module.data_gated_methods) == expected


def test_missing_external_feeds_produce_unknown_non_directional_readings():
    module = ExternalDataGatedReadings()
    readings = module.read("EURUSD", {
        "H1": [{
            "open": 1.08,
            "high": 1.09,
            "low": 1.07,
            "close": 1.085,
            "timestamp_epoch": 1_800_000_000,
            "source_timestamp_verified": True,
        }],
    })

    assert {reading.concept for reading in readings} == set(DATA_GATED_METHODS)
    for reading in readings:
        assert reading.applicable is True
        assert reading.source_timestamp_verified is True
        assert reading.source_bar_epoch == 1_800_000_000
        assert reading.unknowns
        assert reading.implications[0].direction == "FLAT"
        assert reading.implications[0].strength is None
        assert reading.implications[0].activated is False
        assert reading.facts["external_feed_observed"] is False
        assert reading.facts["entry_authorization_emitted"] is False


def test_missing_completed_bars_do_not_fabricate_external_feed_readings():
    assert ExternalDataGatedReadings().read("EURUSD", {"H1": []}) == []


def test_external_feed_gates_emit_one_context_reading_per_symbol_not_per_timeframe():
    bar = {
        "open": 1.08,
        "high": 1.09,
        "low": 1.07,
        "close": 1.085,
        "timestamp_epoch": 1_800_000_000,
        "source_timestamp_verified": True,
    }
    readings = ExternalDataGatedReadings().read("EURUSD", {
        "M5": [{**bar, "timestamp_epoch": 1_800_000_300}],
        "H1": [bar],
    })
    assert len(readings) == len(DATA_GATED_METHODS)
    assert {reading.timeframe for reading in readings} == {"M5"}

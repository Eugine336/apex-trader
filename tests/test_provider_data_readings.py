"""Provider-neutral Brain readers reject stale, unverified and mislabelled external data."""
from data_sources import (
    Completeness, DataObservation, DataSourceRegistry, ObservationKind,
    ObservationProvenance, SourceIdentity,
)
from cognition.concepts.provider_data_readings import ProviderDataReadings
from cognition.concepts.external_data_gated_readings import ExternalDataGatedReadings


CUTOFF = 2_000_000_000.0


def _obs(kind, record_id, values, *, source_epoch=1_999_000_000.0,
         publication_epoch=1_998_000_000.0, volume_basis=None, aggressor_basis=None):
    return DataObservation(
        provenance=ObservationProvenance(
            source=SourceIdentity("fixture", "venue-a", "XAUUSD", "GOLD"),
            kind=kind,
            source_timestamp_epoch=source_epoch,
            received_timestamp_epoch=CUTOFF,
            source_timestamp_verified=True,
            completeness=Completeness.COMPLETE,
            source_record_id=record_id,
            publication_timestamp_epoch=publication_epoch,
            volume_basis=volume_basis,
            aggressor_side_basis=aggressor_basis,
        ),
        values=values,
    )


class _FixtureProvider:
    provider_id = "fixture"

    def __init__(self):
        self.rows = [
            _obs(
                ObservationKind.ECONOMIC_EVENT, "cpi-release",
                {"event_id": "cpi-release", "event_time_epoch": CUTOFF + 3600,
                 "currency": "USD", "event_category": "cpi",
                 "importance": "high", "actual": None, "forecast": 3.1, "previous": 3.2},
                source_epoch=CUTOFF + 3600,
            ),
            _obs(
                ObservationKind.MACRO_SERIES, "fed-rate",
                {"series_id": "policy-rate", "series_family": "policy_rate",
                 "currency": "USD", "observation_epoch": CUTOFF - 86400,
                 "value": 4.5, "unit": "percent"},
            ),
            _obs(
                ObservationKind.DERIVATIVES, "gc-options",
                {"expiry_epoch": CUTOFF + 30 * 86400, "implied_volatility": 0.18,
                 "skew": 0.03, "open_interest": 1200},
            ),
            _obs(
                ObservationKind.FUNDING, "funding-1",
                {"funding_rate": 0.0001},
            ),
            _obs(
                ObservationKind.OPEN_INTEREST, "oi-1",
                {"open_interest": 10000},
            ),
            _obs(
                ObservationKind.LIQUIDATION, "liq-1",
                {"liquidation_quantity": 2.0},
            ),
            _obs(
                ObservationKind.TRADE_PRINT, "trade-buy",
                {"price": 2000.0, "quantity": 2.0, "aggressor_side": "buy"},
                source_epoch=CUTOFF - 100,
                volume_basis="executed_trade_volume", aggressor_basis="exchange_classified",
            ),
            _obs(
                ObservationKind.TRADE_PRINT, "trade-sell",
                {"price": 2000.5, "quantity": 1.0, "aggressor_side": "sell"},
                volume_basis="executed_trade_volume", aggressor_basis="exchange_classified",
            ),
            _obs(
                ObservationKind.EXECUTION_COST, "fill-1",
                {"side": "BUY", "executed_size": 0.1, "quote_bid": 1999.9,
                 "quote_ask": 2000.0, "fill_price": 2000.1,
                 "quote_timestamp_epoch": CUTOFF - 20,
                 "fill_timestamp_epoch": CUTOFF - 10,
                 "commission_account_currency": 0.02, "commission_currency": "USD"},
                source_epoch=CUTOFF - 10,
            ),
        ]

    def capabilities(self):
        return []

    def fetch(self, kind, **kwargs):
        return [row for row in self.rows if row.provenance.kind is kind]


def test_provider_readers_consume_normalized_observations_and_preserve_vintages():
    registry = DataSourceRegistry()
    registry.register(_FixtureProvider())
    module = ProviderDataReadings()
    bars = {"M5": [{
        "open": 1999.0, "high": 2001.0, "low": 1998.0, "close": 2000.0,
        "timestamp_epoch": CUTOFF - 300,
        "source_timestamp_verified": True,
    }]}
    context = {
        "data_source_registry": registry,
        "analysis_epoch": CUTOFF,
        "symbol_spec": {
            "currency_base": "XAU", "currency_profit": "USD",
        },
    }
    readings = module.read_with_context("XAUUSD", bars, context)
    by_concept = {item.concept: item for item in readings}

    assert {
        "event_driven", "macro_growth", "macro_rates", "derivatives",
        "crypto_microstructure", "delta_cvd", "footprint", "market_impact",
    } <= set(by_concept)
    event = by_concept["event_driven"].facts["events"][0]
    assert event["event_time_epoch"] > CUTOFF
    assert event["publication_timestamp_epoch"] <= CUTOFF
    assert by_concept["delta_cvd"].facts["signed_delta"] == 1.0
    assert by_concept["footprint"].facts["price_level_count"] == 2
    assert by_concept["market_impact"].facts["execution_count"] == 1
    assert by_concept["market_impact"].facts["predictive_fill_probability_emitted"] is False
    assert all(item.source_timestamp_verified for item in readings)
    assert all(item.source_timestamp_epoch <= CUTOFF for item in readings)


def test_external_gate_is_suppressed_only_for_provider_concepts_actually_read():
    registry = DataSourceRegistry()
    registry.register(_FixtureProvider())
    bars = {"M5": [{
        "open": 1999.0, "high": 2001.0, "low": 1998.0, "close": 2000.0,
        "timestamp_epoch": CUTOFF - 300, "source_timestamp_verified": True,
    }]}
    context = {
        "data_source_registry": registry, "analysis_epoch": CUTOFF,
        "symbol_spec": {"currency_base": "XAU", "currency_profit": "USD"},
    }
    provider_readings = ProviderDataReadings().read_with_context("XAUUSD", bars, context)
    gated = ExternalDataGatedReadings().read_with_context(
        "XAUUSD", bars, {"concept_readings": tuple(provider_readings)},
    )
    gated_concepts = {item.concept for item in gated}
    assert "event_driven" not in gated_concepts
    assert "macro_rates" not in gated_concepts
    assert "derivatives" not in gated_concepts
    assert "crypto_microstructure" not in gated_concepts
    assert "delta_cvd" not in gated_concepts
    assert "footprint" not in gated_concepts
    assert "market_impact" not in gated_concepts
    assert "synthetic_market" in gated_concepts


def test_provider_reader_does_not_infer_exchange_flow_from_quote_or_dom_records():
    class _ProxyOnly(_FixtureProvider):
        def __init__(self):
            self.rows = [
                _obs(
                    ObservationKind.TRADE_PRINT, "quote-not-a-trade",
                    {"price": 2000.0, "quantity": 10.0, "aggressor_side": "buy"},
                    source_epoch=CUTOFF - 100,
                    volume_basis="tick_volume_proxy", aggressor_basis="tick_rule_estimate",
                ),
            ]
    registry = DataSourceRegistry()
    registry.register(_ProxyOnly())
    bars = {"M5": [{
        "open": 1999.0, "high": 2001.0, "low": 1998.0, "close": 2000.0,
        "timestamp_epoch": CUTOFF - 300, "source_timestamp_verified": True,
    }]}
    readings = ProviderDataReadings().read_with_context(
        "XAUUSD", bars, {"data_source_registry": registry, "analysis_epoch": CUTOFF},
    )
    assert not any(item.concept in {"delta_cvd", "footprint"} for item in readings)


def test_macro_series_revisions_after_analysis_cutoff_are_excluded():
    class _RevisedProvider(_FixtureProvider):
        def __init__(self):
            super().__init__()
            self.rows = [
                row for row in self.rows
                if row.provenance.kind is not ObservationKind.MACRO_SERIES
            ] + [
                DataObservation(
                    provenance=ObservationProvenance(
                        source=SourceIdentity("fixture", "venue-a", "XAUUSD", "GOLD"),
                        kind=ObservationKind.MACRO_SERIES,
                        source_timestamp_epoch=CUTOFF - 100,
                        received_timestamp_epoch=CUTOFF,
                        source_timestamp_verified=True,
                        completeness=Completeness.COMPLETE,
                        source_record_id="revised-after-cutoff",
                        publication_timestamp_epoch=CUTOFF - 200,
                        revision_timestamp_epoch=CUTOFF + 1,
                    ),
                    values={"series_id": "policy-rate", "series_family": "policy_rate",
                            "currency": "USD", "observation_epoch": CUTOFF - 86400,
                            "value": 99.0, "unit": "percent"},
                ),
            ]

    registry = DataSourceRegistry()
    registry.register(_RevisedProvider())
    bars = {"M5": [{
        "open": 1999.0, "high": 2001.0, "low": 1998.0, "close": 2000.0,
        "timestamp_epoch": CUTOFF - 300, "source_timestamp_verified": True,
    }]}
    readings = ProviderDataReadings().read_with_context(
        "XAUUSD", bars,
        {"data_source_registry": registry, "analysis_epoch": CUTOFF,
         "symbol_spec": {"source_verified": True, "specification_verified": True,
                         "currency_base": "XAU", "currency_profit": "USD"}},
    )
    assert not any(item.concept == "macro_rates" for item in readings)


def test_macro_readings_require_verified_instrument_currency_metadata():
    registry = DataSourceRegistry()
    registry.register(_FixtureProvider())
    bars = {"M5": [{
        "open": 1999.0, "high": 2001.0, "low": 1998.0, "close": 2000.0,
        "timestamp_epoch": CUTOFF - 300, "source_timestamp_verified": True,
    }]}
    readings = ProviderDataReadings().read_with_context(
        "XAUUSD", bars,
        {"data_source_registry": registry, "analysis_epoch": CUTOFF,
         "symbol_spec": {"currency_base": "XAU", "currency_profit": "USD"}},
    )
    assert not any(item.concept in {"event_driven", "macro_growth", "macro_rates"} for item in readings)

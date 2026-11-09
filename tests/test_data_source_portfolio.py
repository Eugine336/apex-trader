from __future__ import annotations

from data_sources import (
    Completeness, DataObservation, DataSourceRegistry, ObservationKind,
    ObservationProvenance, SourceIdentity,
)
from data_sources.portfolio import build_portfolio_risk_snapshot


class _PortfolioProvider:
    provider_id = "broker-fixture"

    def __init__(self, *, stamp=1_800_000_000, values=None):
        self.stamp = stamp
        self.values = values or {
            "portfolio_id": "mt5:account-a",
            "complete": True,
            "equity": 10_000.0,
            "confirmed_platforms": ["mt5"],
            "failed_platforms": [],
            "positions": [{
                "position_id": "p1", "symbol": "XAUUSD",
                "source_timestamp_epoch": 1_800_000_000,
                "source_timestamp_verified": True,
                "risk_account_ccy": 100.0,
                "notional_account_ccy": 2_000.0,
                "net_exposure_account_ccy": 2_000.0,
                "currency_exposure": {"USD": 2_000.0},
            }],
            "portfolio_returns": [
                {"source_timestamp_epoch": 1_799_999_000, "source_timestamp_verified": True, "return_fraction": 0.01},
                {"source_timestamp_epoch": 1_799_999_500, "source_timestamp_verified": True, "return_fraction": -0.005},
            ],
            "equity_curve": [
                {"source_timestamp_epoch": 1_799_999_000, "source_timestamp_verified": True, "equity": 9_900.0},
                {"source_timestamp_epoch": 1_799_999_500, "source_timestamp_verified": True, "equity": 10_000.0},
            ],
            "correlation_pairs": [],
        }

    def capabilities(self):
        return []

    def fetch(self, kind, **kwargs):
        if kind is not ObservationKind.PORTFOLIO_SNAPSHOT:
            return []
        return [DataObservation(
            provenance=ObservationProvenance(
                source=SourceIdentity(
                    "broker-fixture", "mt5", "PORTFOLIO", "mt5:account-a",
                ),
                kind=kind,
                source_timestamp_epoch=self.stamp,
                received_timestamp_epoch=self.stamp,
                source_timestamp_verified=True,
                completeness=Completeness.COMPLETE,
                source_record_id="snapshot-1",
            ),
            values=self.values,
        )]


def test_portfolio_snapshot_adapter_produces_only_atomic_verified_snapshot():
    registry = DataSourceRegistry()
    registry.register(_PortfolioProvider())
    result = build_portfolio_risk_snapshot(
        registry, analysis_epoch=1_800_000_100,
        completed_bar_epoch=1_799_999_900,
    )
    assert result["complete"] is True
    assert result["portfolio_id"] == "mt5:account-a"
    assert result["equity"] == 10_000.0
    assert result["positions"][0]["position_id"] == "p1"
    assert result["source_provider"] == "broker-fixture"


def test_portfolio_snapshot_rejects_stale_snapshot_and_future_snapshot():
    stale = DataSourceRegistry()
    stale.register(_PortfolioProvider(stamp=1_799_999_800))
    old = build_portfolio_risk_snapshot(
        stale, analysis_epoch=1_800_000_100,
        completed_bar_epoch=1_799_999_900,
    )
    assert old["complete"] is False
    assert "predates" in " ".join(old["unknowns"])

    future = DataSourceRegistry()
    future.register(_PortfolioProvider(stamp=1_800_000_200))
    ahead = build_portfolio_risk_snapshot(
        future, analysis_epoch=1_800_000_100,
        completed_bar_epoch=1_799_999_900,
    )
    assert ahead["complete"] is False
    assert "exactly one" in " ".join(ahead["unknowns"])


def test_portfolio_snapshot_provider_failure_is_visible_and_incomplete():
    class _Broken(_PortfolioProvider):
        provider_id = "broken"
        def fetch(self, kind, **kwargs):
            raise ConnectionError("unavailable")

    registry = DataSourceRegistry()
    registry.register(_Broken())
    result = build_portfolio_risk_snapshot(
        registry, analysis_epoch=1_800_000_100,
        completed_bar_epoch=1_799_999_900,
    )
    assert result["complete"] is False
    assert result["provider_errors"] == {"broken": "ConnectionError"}
    assert result["unknowns"]


def test_mt5_adapter_integrates_sanitized_live_partial_evidence_without_claiming_verification():
    from data_sources.mt5_portfolio import MT5PortfolioAdapter

    class Connector:
        def get_account_snapshot(self):
            return {
                "source_verified": True, "login": 123456, "password": "never-copy",
                "equity": 2500.0, "currency": "USD", "leverage": 100,
            }

        def get_position_snapshots(self):
            return [{"ticket": 9988, "symbol": "EURUSD", "volume": 0.2, "profit": 3.0}]

    class Manager:
        mt5_connectors = [Connector()]
        _mt5_connected_flags = [True]

    adapter = MT5PortfolioAdapter(Manager())
    (observation,) = adapter.fetch(ObservationKind.PORTFOLIO_SNAPSHOT)
    assert observation.provenance.completeness is Completeness.PARTIAL
    assert observation.provenance.source_timestamp_verified is False
    assert observation.provenance.source_timestamp_epoch is None
    assert observation.values["account_observations"] == [{"equity": 2500.0, "currency": "USD", "leverage": 100}]
    assert observation.values["positions_observed"] == [{"symbol": "EURUSD", "volume": 0.2, "profit": 3.0}]
    assert "password" not in repr(observation.values)
    assert "login" not in repr(observation.values)
    assert "ticket" not in repr(observation.values)

    registry = DataSourceRegistry()
    registry.register(adapter)
    result = build_portfolio_risk_snapshot(
        registry, analysis_epoch=1_800_000_100, completed_bar_epoch=1_799_999_900,
    )
    assert result["complete"] is False
    assert result["source_timestamp_verified"] is False
    assert result["partial_observation"]["account_observations"][0]["equity"] == 2500.0


def test_portfolio_reader_surfaces_partial_mt5_evidence_as_data_gated():
    from data_sources.mt5_portfolio import MT5PortfolioAdapter
    from cognition.concepts.portfolio_risk_readings import PortfolioRiskReadings

    class Connector:
        def get_account_snapshot(self):
            return {"source_verified": True, "equity": 2500.0}

        def get_position_snapshots(self):
            return []

    class Manager:
        mt5_connectors = [Connector()]
        _mt5_connected_flags = [True]

    registry = DataSourceRegistry()
    registry.register(MT5PortfolioAdapter(Manager()))
    bars = {"M5": [{
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
        "timestamp_epoch": 1_799_999_900, "source_timestamp_verified": True,
    }]}
    (reading,) = PortfolioRiskReadings().read_with_context(
        "XAUUSD", bars, {"data_source_registry": registry, "analysis_epoch": 1_800_000_100},
    )
    assert reading.applicable is False
    assert reading.facts["portfolio_snapshot_verified"] is False
    assert reading.facts["partial_portfolio_observation_available"] is True
    assert reading.facts["account_equity"] is None
    assert reading.facts["partial_account_observations"] == [{"equity": 2500.0}]


def test_portfolio_snapshot_rejects_complete_snapshot_ambiguous_with_partial_source():
    from data_sources.mt5_portfolio import MT5PortfolioAdapter

    class Connector:
        def get_account_snapshot(self):
            return {"source_verified": True, "equity": 2500.0}

        def get_position_snapshots(self):
            return []

    class Manager:
        mt5_connectors = [Connector()]
        _mt5_connected_flags = [True]

    registry = DataSourceRegistry()
    registry.register(_PortfolioProvider())
    registry.register(MT5PortfolioAdapter(Manager()))
    result = build_portfolio_risk_snapshot(
        registry, analysis_epoch=1_800_000_100, completed_bar_epoch=1_799_999_900,
    )
    assert result["complete"] is False
    assert result["source_timestamp_verified"] is False
    assert any("across 2 observation(s)" in item for item in result["unknowns"])
    assert any("cannot be combined safely" in item for item in result["unknowns"])



def test_portfolio_risk_reader_consumes_registered_broker_snapshot():
    from cognition.concepts.portfolio_risk_readings import PortfolioRiskReadings

    registry = DataSourceRegistry()
    registry.register(_PortfolioProvider())
    bars = {"M5": [{
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
        "timestamp_epoch": 1_799_999_900,
        "source_timestamp_verified": True,
    }]}
    (reading,) = PortfolioRiskReadings().read_with_context(
        "XAUUSD", bars,
        {"data_source_registry": registry, "analysis_epoch": 1_800_000_100},
    )
    assert reading.applicable is True
    assert reading.facts["portfolio_snapshot_verified"] is True
    assert reading.facts["account_equity"] == 10_000.0
    assert reading.facts["open_risk_account_ccy"] == 100.0

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
                    "broker-fixture", "mt5", "PORTFOLIO", "account-a",
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

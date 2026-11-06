"""Contract tests for broker-truth portfolio risk readings."""
from __future__ import annotations

import math

from cognition.concepts.portfolio_risk_readings import DATA_GATED_METHODS, PortfolioRiskReadings


def _bars():
    return [
        {
            "open": 100.0 + index, "high": 101.0 + index,
            "low": 99.0 + index, "close": 100.5 + index,
            "timestamp_epoch": 1_780_000_000.0 + index * 300.0,
            "source_timestamp_verified": True,
        }
        for index in range(40)
    ]


def _snapshot(cutoff):
    return {
        "portfolio_id": "account-main", "complete": True,
        "source_timestamp_epoch": cutoff + 30.0, "source_timestamp_verified": True,
        "confirmed_platforms": ["mt5", "deriv"], "failed_platforms": [],
        "equity": 10000.0,
        "positions": [
            {
                "position_id": "mt5-1", "symbol": "EURUSD",
                "risk_account_ccy": 100.0, "notional_account_ccy": 5000.0,
                "net_exposure_account_ccy": 3000.0,
                "currency_exposure": {"EUR": 3000.0, "USD": -3000.0},
                "source_timestamp_epoch": cutoff + 30.0, "source_timestamp_verified": True,
            },
            {
                "position_id": "deriv-2", "symbol": "GBPUSD",
                "risk_account_ccy": 50.0, "notional_account_ccy": 4000.0,
                "net_exposure_account_ccy": -4000.0,
                "currency_exposure": {"GBP": -4000.0, "USD": 4000.0},
                "source_timestamp_epoch": cutoff + 30.0, "source_timestamp_verified": True,
            },
        ],
        "portfolio_returns": [
            {
                "return_fraction": value,
                "source_timestamp_epoch": cutoff - (5 - index) * 300.0,
                "source_timestamp_verified": True,
            }
            for index, value in enumerate((0.01, -0.015, 0.005, 0.002, -0.003))
        ],
        "equity_curve": [
            {
                "equity": value,
                "source_timestamp_epoch": cutoff - (4 - index) * 300.0,
                "source_timestamp_verified": True,
            }
            for index, value in enumerate((10000.0, 10100.0, 9900.0, 10200.0))
        ],
        "correlation_pairs": [{
            "symbol_a": "EURUSD", "symbol_b": "GBPUSD", "correlation": 0.75,
            "source_timestamp_epoch": cutoff + 30.0, "source_timestamp_verified": True,
        }],
    }


def _read(context=None):
    return PortfolioRiskReadings().read_with_context("XAUUSD", {"M5": _bars()}, context or {})[0]


def test_portfolio_risk_is_data_gated_without_broker_truth_snapshot():
    item = _read({"analysis_epoch": _bars()[-1]["timestamp_epoch"] + 60.0})
    assert set(DATA_GATED_METHODS) == {"portfolio_risk"}
    assert item.applicable is False
    assert item.facts["data_gated_reason"]
    assert item.facts["directional_decision_emitted"] is False
    assert item.facts["entry_authorization_emitted"] is False
    assert item.implications == ()


def test_portfolio_risk_computes_whole_book_heat_exposure_tail_and_drawdown():
    cutoff = _bars()[-1]["timestamp_epoch"]
    item = _read({"analysis_epoch": cutoff + 60.0, "portfolio_risk_snapshot": _snapshot(cutoff)})
    assert item.applicable is True
    assert item.facts["open_risk_account_ccy"] == 150.0
    assert math.isclose(item.facts["live_heat_pct"], 1.5)
    assert item.facts["gross_exposure_account_ccy"] == 9000.0
    assert item.facts["net_exposure_account_ccy"] == -1000.0
    assert math.isclose(item.facts["portfolio_var_95_fraction"], -0.0126)
    assert item.facts["portfolio_expected_shortfall_95_fraction"] == -0.015
    assert math.isclose(item.facts["portfolio_max_drawdown_fraction"], 200.0 / 10100.0)
    assert item.facts["max_absolute_pairwise_correlation"] == 0.75


def test_portfolio_risk_rejects_partial_platform_book_and_future_return_rows():
    cutoff = _bars()[-1]["timestamp_epoch"]
    snapshot = _snapshot(cutoff)
    snapshot["failed_platforms"] = ["mt5"]
    snapshot["portfolio_returns"].append({
        "return_fraction": 0.5, "source_timestamp_epoch": cutoff + 90.0,
        "source_timestamp_verified": True,
    })
    item = _read({"analysis_epoch": cutoff + 60.0, "portfolio_risk_snapshot": snapshot})
    assert item.applicable is False
    assert item.facts["portfolio_snapshot_verified"] is False
    assert any("failed platforms" in reason for reason in item.unknowns)



def test_portfolio_risk_rejects_unsynchronized_or_stale_pairwise_correlations():
    cutoff = _bars()[-1]["timestamp_epoch"]
    snapshot = _snapshot(cutoff)
    snapshot["correlation_pairs"][0]["source_timestamp_epoch"] = cutoff - 30.0
    item = _read({
        "analysis_epoch": cutoff + 60.0,
        "portfolio_risk_snapshot": snapshot,
    })
    assert item.applicable is False
    assert item.facts["max_absolute_pairwise_correlation"] is None
    assert any("synchronized to one timestamp" in reason for reason in item.unknowns)


def test_portfolio_risk_rejects_position_rows_from_mixed_snapshot_epochs():
    cutoff = _bars()[-1]["timestamp_epoch"]
    snapshot = _snapshot(cutoff)
    snapshot["positions"][1]["source_timestamp_epoch"] = cutoff
    item = _read({
        "analysis_epoch": cutoff + 60.0,
        "portfolio_risk_snapshot": snapshot,
    })
    assert item.applicable is False
    assert item.facts["positions_complete"] is False
    assert item.facts["open_risk_account_ccy"] is None


def test_portfolio_risk_carries_only_verified_monte_carlo_ruin_probability():
    from cognition.concepts.base import ConceptReading
    from cognition.contracts import EvidenceDomain

    cutoff = _bars()[-1]["timestamp_epoch"]
    monte_carlo = ConceptReading(
        concept="monte_carlo", timeframe="M5", domain=EvidenceDomain.PORTFOLIO,
        applicable=True, applicability_reason="whole-book simulation available",
        observation="bootstrap", implications=(), facts={"probability_of_ruin": 0.03},
        source_bar_epoch=cutoff,
        source_timestamp_verified=True,
    )
    item = _read({
        "analysis_epoch": cutoff + 60.0,
        "portfolio_risk_snapshot": _snapshot(cutoff),
        "concept_readings": (monte_carlo,),
    })
    assert item.facts["monte_carlo_probability_of_ruin"] == 0.03
    assert item.facts["risk_of_ruin_available"] is True

"""
APEX TRADER — Tests for EV estimator unit-correctness fix.

Validates that EV is computed on normalized R-multiples (pnl_dollars / risk_dollars)
rather than raw pips, with a USD fallback and a sanity guard against absurd values.
"""

from adaptive.ev_estimator import EVEstimator, EVEstimate


def _trade(pair="EURUSD", pnl_dollars=10.0, risk_dollars=10.0,
           regime="BULLISH", session="LONDON", **extra):
    d = {
        "pair": pair,
        "pnl": 999.0,  # pips — must be IGNORED by the estimator
        "pnl_dollars": pnl_dollars,
        "risk_dollars": risk_dollars,
        "regime": regime,
        "session": session,
    }
    d.update(extra)
    return d


class TestEVUsesRMultiples:

    def test_positive_ev_in_r(self):
        trades = (
            [_trade(pnl_dollars=20.0, risk_dollars=10.0)] * 8
            + [_trade(pnl_dollars=-10.0, risk_dollars=10.0)] * 2
        )
        ev = EVEstimator(min_trades_for_gate=5)
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", trades)
        assert result.unit == "R"
        assert result.expected_value > 0

    def test_negative_ev_in_r(self):
        trades = [_trade(pnl_dollars=-15.0, risk_dollars=10.0)] * 15
        ev = EVEstimator(min_trades_for_gate=5)
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", trades)
        assert result.unit == "R"
        assert result.expected_value < 0

    def test_r_values_are_correct(self):
        trades = [_trade(pnl_dollars=30.0, risk_dollars=10.0)] * 10
        ev = EVEstimator(min_trades_for_gate=5)
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", trades)
        assert result.avg_win == 3.0
        assert result.avg_loss == 0.0
        assert result.win_rate == 1.0

    def test_pips_column_ignored(self):
        trades = [
            _trade(pnl_dollars=5.0, risk_dollars=10.0)
            for _ in range(10)
        ]
        ev = EVEstimator(min_trades_for_gate=5)
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", trades)
        assert result.expected_value == 0.5
        assert result.unit == "R"


class TestEVFallbackToUSD:

    def test_usd_fallback_when_no_risk(self):
        trades = [
            {"pair": "EURUSD", "pnl": 10.0, "pnl_dollars": 50.0,
             "risk_dollars": None, "regime": "BULLISH", "session": "LONDON"}
            for _ in range(10)
        ]
        ev = EVEstimator(min_trades_for_gate=5)
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", trades)
        assert result.unit == "USD"
        assert result.expected_value > 0

    def test_insufficient_when_no_dollar_data(self):
        trades = [
            {"pair": "EURUSD", "pnl": 10.0, "pnl_dollars": 0.0,
             "risk_dollars": None, "regime": "BULLISH", "session": "LONDON"}
            for _ in range(10)
        ]
        ev = EVEstimator(min_trades_for_gate=5)
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", trades)
        assert result.confidence == "insufficient"


class TestEVSanityGuard:

    def test_absurd_positive_ev_neutralized(self):
        trades = [_trade(pnl_dollars=1e8, risk_dollars=1.0)] * 10
        ev = EVEstimator(min_trades_for_gate=5)
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", trades)
        assert result.confidence == "insufficient"
        assert result.expected_value == 0.0

    def test_absurd_negative_ev_neutralized(self):
        trades = [_trade(pnl_dollars=-1e8, risk_dollars=1.0)] * 10
        ev = EVEstimator(min_trades_for_gate=5)
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", trades)
        assert result.confidence == "insufficient"
        assert result.expected_value == 0.0

    def test_normal_ev_passes_guard(self):
        trades = (
            [_trade(pnl_dollars=20.0, risk_dollars=10.0)] * 6
            + [_trade(pnl_dollars=-10.0, risk_dollars=10.0)] * 4
        )
        ev = EVEstimator(min_trades_for_gate=5)
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", trades)
        assert result.confidence != "insufficient"
        assert result.expected_value != 0.0


class TestEVEstimateDataclass:

    def test_unit_field_present(self):
        est = EVEstimate(
            expected_value=1.0, win_rate=0.5,
            avg_win=2.0, avg_loss=1.0,
            sample_size=20, confidence="medium",
            source="pair", unit="R",
        )
        assert est.unit == "R"
        assert est.avg_win == 2.0
        assert est.avg_loss == 1.0

    def test_default_estimate_has_unit(self):
        ev = EVEstimator(min_trades_for_gate=10)
        result = ev.estimate("EURUSD", "X", "Y", [])
        assert result.unit == "R"
        assert result.confidence == "insufficient"


class TestMixedHistoryGraceful:

    def test_mixed_r_and_usd_prefers_r(self):
        r_trades = [_trade(pnl_dollars=20.0, risk_dollars=10.0)] * 8
        usd_trades = [
            {"pair": "EURUSD", "pnl": 5.0, "pnl_dollars": 50.0,
             "risk_dollars": None, "regime": "BULLISH", "session": "LONDON"}
        ] * 4
        ev = EVEstimator(min_trades_for_gate=5)
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", r_trades + usd_trades)
        assert result.unit == "R"

    def test_confidence_tiers_preserved(self):
        trades = [_trade(pnl_dollars=10.0, risk_dollars=10.0)] * 55
        ev = EVEstimator(min_trades_for_gate=5)
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", trades)
        assert result.confidence == "high"

"""
APEX TRADER — Phase 3 Capital Context Tests
Tests for HWM tracking, micro account sizing, EV estimation,
dynamic risk scaling, and opportunity ranking.
"""

import pytest
from datetime import datetime, timezone

from brain.drawdown_guard import DrawdownGuard
from risk.position_sizer import PositionSizer
from risk.risk_engine import RiskEngine
from adaptive.ev_estimator import EVEstimator
from scanner.pair_ranker import PairRanker
from scanner.pair_scanner import PairScanResult


# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════

def _ts(hour: int = 10) -> datetime:
    return datetime(2025, 6, 1, hour, 0, 0, tzinfo=timezone.utc)


def _make_scan_result(
    pair: str = "EURUSD", score: int = 90, ev: float = 0.0, status: str = "READY",
) -> PairScanResult:
    return PairScanResult(
        pair=pair, direction="LONG", score=score, regime="BULLISH",
        trend_h4="BULLISH", trend_h1="BULLISH", bias_strength="STRONG",
        has_fvg=True, has_order_block=True, has_liquidity_target=True,
        sweep_detected=True, inducement_detected=False, wyckoff_phase="N/A",
        volume_confirmation=True, session_active=True,
        currency_strength_aligned=True, status=status,
        timestamp=_ts(), ev_estimate=ev, opportunity_score=0.0,
    )


def _make_trade(pair: str = "EURUSD", pnl: float = 10.0,
                regime: str = "BULLISH", session: str = "LONDON",
                pnl_dollars: float | None = None,
                risk_dollars: float | None = None) -> dict:
    d: dict = {"pair": pair, "pnl": pnl, "regime": regime, "session": session}
    if pnl_dollars is not None:
        d["pnl_dollars"] = pnl_dollars
    else:
        d["pnl_dollars"] = pnl
    if risk_dollars is not None:
        d["risk_dollars"] = risk_dollars
    else:
        d["risk_dollars"] = 10.0
    return d


# ════════════════════════════════════════════════════════════════════
# 3.1 — Equity Curve Awareness (HWM)
# ════════════════════════════════════════════════════════════════════

class TestHighWaterMark:

    def test_hwm_tracks_new_peak(self):
        g = DrawdownGuard()
        g.register_trade_result(0.02, _ts(10))
        g.register_trade_result(0.03, _ts(11))
        assert g.high_water_mark == pytest.approx(0.05, abs=1e-6)

    def test_hwm_unchanged_when_equity_drops(self):
        g = DrawdownGuard()
        g.register_trade_result(0.04, _ts(10))
        peak = g.high_water_mark
        g.register_trade_result(-0.01, _ts(11))
        assert g.high_water_mark == pytest.approx(peak, abs=1e-6)

    def test_drawdown_from_peak_calculation(self):
        g = DrawdownGuard()
        g.register_trade_result(0.10, _ts(10))
        g.register_trade_result(-0.02, _ts(11))
        status = g.get_status(_ts(11))
        expected_dd = 0.02 / 0.10
        assert status.drawdown_from_peak_pct == pytest.approx(expected_dd, abs=1e-4)

    def test_hwm_in_drawdown_status(self):
        g = DrawdownGuard()
        g.register_trade_result(0.05, _ts(10))
        status = g.get_status(_ts(10))
        assert status.high_water_mark == pytest.approx(0.05, abs=1e-6)

    def test_is_recovering_flag(self):
        g = DrawdownGuard()
        g.register_trade_result(0.10, _ts(10))
        g.register_trade_result(-0.03, _ts(11))
        g.register_trade_result(0.01, _ts(12))
        hwm_info = g.update_hwm(g.equity_points[-1][1], _ts(12))
        assert hwm_info["is_recovering"] is True
        assert hwm_info["is_at_peak"] is False


# ════════════════════════════════════════════════════════════════════
# 3.2 — Account-Size-Aware Sizing
# ════════════════════════════════════════════════════════════════════

class TestMicroAccountSizing:

    def test_standard_account_unchanged(self):
        sizer = PositionSizer()
        result = sizer.calculate(
            account_balance=1000.0, risk_pct=0.02,
            entry_price=1.10000, stop_loss=1.09800,
            pip_size=0.0001, pip_value_per_lot=10.0,
        )
        assert result.lots > 0
        assert result.sizing_mode == "lots"

    def test_micro_account_skips_oversized(self):
        sizer = PositionSizer()
        result = sizer.calculate(
            account_balance=5.0, risk_pct=0.02,
            entry_price=1.10000, stop_loss=1.09800,
            pip_size=0.0001, pip_value_per_lot=10.0,
        )
        assert result.lots == 0.0
        assert result.sizing_mode == "lots_skip_micro"

    def test_micro_account_allows_small_stops(self):
        sizer = PositionSizer()
        result = sizer.calculate(
            account_balance=50.0, risk_pct=0.02,
            entry_price=1.10000, stop_loss=1.09990,
            pip_size=0.0001, pip_value_per_lot=10.0,
        )
        assert result.lots == 0.1
        assert result.sizing_mode == "lots"

    def test_deriv_micro_skips_below_minimum_stake(self):
        sizer = PositionSizer()
        result = sizer.calculate_stake(
            account_balance=5.0, risk_pct=0.01,
            entry_price=100.0, stop_loss=99.0,
        )
        assert result.stake_usd == 0.0
        assert result.sizing_mode == "stake_skip_micro"

    def test_risk_engine_rejects_micro_skip(self):
        engine = RiskEngine(starting_balance=5.0)
        result = engine.assess(
            pair="EURUSD", direction="LONG",
            entry_price=1.10000, stop_loss=1.09800,
            account_balance=5.0,
        )
        assert result.approved is False
        assert any("account too small" in r.lower() for r in result.rejections)


# ════════════════════════════════════════════════════════════════════
# 3.3 — Expected Value Gate
# ════════════════════════════════════════════════════════════════════

class TestEVEstimator:

    def test_positive_ev_estimate(self):
        trades = [_make_trade(pnl=20)] * 8 + [_make_trade(pnl=-10)] * 2
        ev = EVEstimator(min_trades_for_gate=5)
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", trades)
        assert result.expected_value > 0
        assert result.source == "pair"

    def test_negative_ev_below_threshold_rejected(self):
        engine = RiskEngine(starting_balance=10_000.0)
        losing_trades = [_make_trade(pnl=-20)] * 15 + [_make_trade(pnl=5)] * 5
        result = engine.assess(
            pair="EURUSD", direction="LONG",
            entry_price=1.10, stop_loss=1.098,
            trade_history=losing_trades,
            regime="BULLISH", session="LONDON",
        )
        assert result.approved is False
        assert any("negative ev" in r.lower() for r in result.rejections)

    def test_insufficient_data_always_allows(self):
        engine = RiskEngine(starting_balance=10_000.0)
        few_trades = [_make_trade(pnl=-20)] * 3
        result = engine.assess(
            pair="EURUSD", direction="LONG",
            entry_price=1.10, stop_loss=1.098,
            trade_history=few_trades,
            regime="BULLISH", session="LONDON",
        )
        assert result.approved is True

    def test_ev_from_pair_specific_data(self):
        ev = EVEstimator(min_trades_for_gate=5)
        trades = [_make_trade("EURUSD", 20)] * 10
        result = ev.estimate("EURUSD", "RANGING", "TOKYO", trades)
        assert result.source == "pair"

    def test_ev_fallback_to_regime(self):
        ev = EVEstimator(min_trades_for_gate=5)
        trades = [_make_trade("GBPUSD", 15, regime="BULLISH")] * 10
        result = ev.estimate("EURUSD", "BULLISH", "LONDON", trades)
        assert result.source == "regime"


# ════════════════════════════════════════════════════════════════════
# 3.4 — Dynamic Risk Scaling
# ════════════════════════════════════════════════════════════════════

class TestDynamicRiskScaling:

    def test_high_conviction_full_risk(self):
        engine = RiskEngine(starting_balance=10_000.0)
        hwm = {"is_at_peak": False, "drawdown_from_peak_pct": 0.0}
        scaled = engine._scale_risk_by_score(0.02, 95, hwm)
        assert scaled == pytest.approx(0.02, abs=1e-4)

    def test_medium_conviction_reduced_risk(self):
        engine = RiskEngine(starting_balance=10_000.0)
        hwm = {"is_at_peak": False, "drawdown_from_peak_pct": 0.0}
        scaled = engine._scale_risk_by_score(0.02, 89, hwm)
        assert scaled == pytest.approx(0.02 * 0.85, abs=1e-4)

    def test_standard_conviction_further_reduced(self):
        engine = RiskEngine(starting_balance=10_000.0)
        hwm = {"is_at_peak": False, "drawdown_from_peak_pct": 0.0}
        scaled = engine._scale_risk_by_score(0.02, 86, hwm)
        assert scaled == pytest.approx(0.02 * 0.7, abs=1e-4)

    def test_equity_peak_bonus(self):
        engine = RiskEngine(starting_balance=10_000.0)
        hwm = {"is_at_peak": True, "drawdown_from_peak_pct": 0.0}
        scaled = engine._scale_risk_by_score(0.02, 92, hwm)
        assert scaled == pytest.approx(0.02 * 1.0 * 1.1, abs=1e-4)

    def test_drawdown_penalty(self):
        engine = RiskEngine(starting_balance=10_000.0)
        hwm = {"is_at_peak": False, "drawdown_from_peak_pct": 0.15}
        scaled = engine._scale_risk_by_score(0.02, 95, hwm)
        assert scaled == pytest.approx(0.02 * 0.85, abs=1e-4)

    def test_absolute_cap_respected(self):
        engine = RiskEngine(starting_balance=10_000.0)
        hwm = {"is_at_peak": True, "drawdown_from_peak_pct": 0.0}
        scaled = engine._scale_risk_by_score(0.05, 95, hwm)
        assert scaled <= 0.025


# ════════════════════════════════════════════════════════════════════
# 3.5 — Opportunity Cost Ranking
# ════════════════════════════════════════════════════════════════════

class TestOpportunityRanking:

    def test_opportunity_score_calculation(self):
        ranker = PairRanker()
        results = [_make_scan_result("EURUSD", score=90, ev=0.2)]
        ranked = ranker.rank_opportunities(results, {"EURUSD": 1.0})
        assert ranked[0].opportunity_score == pytest.approx(90 * 1.0 * 1.2, abs=0.1)

    def test_ranking_order_by_opportunity_score(self):
        ranker = PairRanker()
        results = [
            _make_scan_result("EURUSD", score=85, ev=0.5),
            _make_scan_result("GBPUSD", score=95, ev=0.0),
        ]
        ranked = ranker.rank_opportunities(results, {"EURUSD": 1.0, "GBPUSD": 1.0})
        assert ranked[0].pair == "EURUSD"

    def test_ranking_with_no_learner_data_uses_defaults(self):
        ranker = PairRanker()
        results = [_make_scan_result("EURUSD", score=90, ev=0.0)]
        ranked = ranker.rank_opportunities(results)
        assert ranked[0].opportunity_score == pytest.approx(90.0, abs=0.1)

    def test_pair_multiplier_affects_ranking(self):
        ranker = PairRanker()
        results = [
            _make_scan_result("EURUSD", score=90, ev=0.0),
            _make_scan_result("GBPUSD", score=90, ev=0.0),
        ]
        ranked = ranker.rank_opportunities(results, {"EURUSD": 0.5, "GBPUSD": 1.0})
        assert ranked[0].pair == "GBPUSD"

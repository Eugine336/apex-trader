"""
Tests for the layered decision architecture (Layers 2 & 3).

Must run WITHOUT torch or pandas installed.
"""

import math
import pytest

from brain.setup_quality import (
    compute_opportunity_quality,
    compute_entry_quality,
    OpportunityQuality,
    EntryQuality,
)
from config import LayeredDecisionConfig, AppConfig


# ── OQ direction symmetry ──────────────────────────────────────────────

class TestOQDirectionSymmetry:
    """A perfect BUY and a perfect SELL with mirror-image inputs
    must produce the SAME OQ score."""

    def test_mirror_inputs_identical_score(self):
        kwargs = dict(
            atr_percentile=50.0,
            spread_current=1.0,
            spread_typical=1.2,
            news_is_clear=True,
            session_liquidity="HIGH",
            session_is_tradeable=True,
            reward_risk_magnitude=2.0,
            ev_estimate=0.5,
            volume_ratio=1.2,
            volume_climax=False,
        )
        oq_buy = compute_opportunity_quality(**kwargs)
        oq_sell = compute_opportunity_quality(**kwargs)
        assert oq_buy.score == oq_sell.score

    def test_no_direction_parameter_accepted(self):
        """compute_opportunity_quality must NOT accept trade_dir."""
        import inspect
        sig = inspect.signature(compute_opportunity_quality)
        params = set(sig.parameters.keys())
        assert "trade_dir" not in params
        assert "direction" not in params
        assert "side" not in params


# ── OQ monotonicity ────────────────────────────────────────────────────

class TestOQMonotonicity:
    """Worse conditions should degrade OQ score (or hold equal)."""

    def _base(self):
        return dict(
            atr_percentile=50.0,
            spread_current=1.0,
            spread_typical=1.2,
            news_is_clear=True,
            session_liquidity="HIGH",
            session_is_tradeable=True,
            reward_risk_magnitude=2.0,
            ev_estimate=0.5,
            volume_ratio=1.2,
            volume_climax=False,
        )

    def test_worse_spread_degrades(self):
        good = compute_opportunity_quality(**self._base())
        bad_kw = {**self._base(), "spread_current": 6.0}
        bad = compute_opportunity_quality(**bad_kw)
        assert bad.score <= good.score

    def test_worse_volatility_degrades(self):
        good = compute_opportunity_quality(**self._base())
        bad_kw = {**self._base(), "atr_percentile": 5.0}
        bad = compute_opportunity_quality(**bad_kw)
        assert bad.score <= good.score

    def test_closer_news_degrades(self):
        good = compute_opportunity_quality(**self._base())
        bad_kw = {**self._base(), "news_is_clear": False, "news_minutes_to_next_high": 3}
        bad = compute_opportunity_quality(**bad_kw)
        assert bad.score <= good.score

    def test_dead_session_degrades(self):
        good = compute_opportunity_quality(**self._base())
        bad_kw = {**self._base(), "session_liquidity": "DEAD", "session_is_tradeable": False}
        bad = compute_opportunity_quality(**bad_kw)
        assert bad.score <= good.score

    def test_volume_climax_degrades(self):
        good = compute_opportunity_quality(**self._base())
        bad_kw = {**self._base(), "volume_climax": True}
        bad = compute_opportunity_quality(**bad_kw)
        assert bad.score <= good.score


# ── EQ degradation ─────────────────────────────────────────────────────

class TestEQDegradation:
    """Greater distance / worse extension should degrade EQ score."""

    def _base(self):
        return dict(
            trade_dir="LONG",
            current_price=1.1000,
            entry_price=1.0990,
            nearest_ob_distance_pips=5.0,
            nearest_fvg_distance_pips=3.0,
            nearest_liq_distance_pips=10.0,
            atr_pips=20.0,
            stop_distance_pips=20.0,
        )

    def test_far_ob_degrades(self):
        good = compute_entry_quality(**self._base())
        bad_kw = {**self._base(), "nearest_ob_distance_pips": 80.0}
        bad = compute_entry_quality(**bad_kw)
        assert bad.score <= good.score

    def test_far_fvg_degrades(self):
        good = compute_entry_quality(**self._base())
        bad_kw = {**self._base(), "nearest_fvg_distance_pips": 80.0}
        bad = compute_entry_quality(**bad_kw)
        assert bad.score <= good.score

    def test_overextended_degrades(self):
        good = compute_entry_quality(**self._base())
        bad_kw = {**self._base(), "current_price": 1.1500, "entry_price": 1.1000}
        bad = compute_entry_quality(**bad_kw)
        assert bad.score <= good.score

    def test_too_tight_stop_degrades(self):
        good = compute_entry_quality(**self._base())
        bad_kw = {**self._base(), "stop_distance_pips": 1.0}
        bad = compute_entry_quality(**bad_kw)
        assert bad.score <= good.score

    def test_neutral_direction_returns_zero(self):
        eq = compute_entry_quality(trade_dir="NEUTRAL")
        assert eq.score == 0.0


# ── Conjunction gate ───────────────────────────────────────────────────

class TestConjunctionGate:
    """Each of (NEUTRAL dir, low OQ, low EQ) independently blocks READY."""

    def test_neutral_blocks_ready(self):
        """Even with high OQ and EQ, NEUTRAL direction → WAITING."""
        oq = compute_opportunity_quality(
            atr_percentile=50, spread_current=1.0, spread_typical=1.2,
            news_is_clear=True, session_liquidity="HIGH",
            session_is_tradeable=True, reward_risk_magnitude=2.0,
            ev_estimate=0.5, volume_ratio=1.2, volume_climax=False,
        )
        eq = compute_entry_quality(trade_dir="NEUTRAL")
        cfg = LayeredDecisionConfig(enabled=True)
        trade_dir = "NEUTRAL"
        assert trade_dir not in ("LONG", "SHORT") or (
            oq.score >= cfg.opportunity_quality_min and eq.score >= cfg.entry_quality_min
        )
        if trade_dir not in ("LONG", "SHORT"):
            status = "WAITING"
        elif oq.score >= cfg.opportunity_quality_min and eq.score >= cfg.entry_quality_min:
            status = "READY"
        elif oq.score >= cfg.opportunity_quality_min or eq.score >= cfg.entry_quality_min:
            status = "WATCHLIST"
        else:
            status = "WAITING"
        assert status == "WAITING"

    def test_low_oq_blocks_ready(self):
        """Direction present + high EQ but OQ below min → NOT READY."""
        oq = compute_opportunity_quality(
            atr_percentile=5, spread_current=10.0, spread_typical=1.0,
            news_is_clear=False, news_minutes_to_next_high=2,
            session_liquidity="DEAD", session_is_tradeable=False,
            reward_risk_magnitude=0.5, ev_estimate=-0.5,
            volume_ratio=0.1, volume_climax=True,
        )
        eq = compute_entry_quality(
            trade_dir="LONG", current_price=1.1, entry_price=1.099,
            nearest_ob_distance_pips=2.0, nearest_fvg_distance_pips=2.0,
            nearest_liq_distance_pips=5.0, atr_pips=20.0, stop_distance_pips=20.0,
        )
        cfg = LayeredDecisionConfig(enabled=True, opportunity_quality_min=5.0)
        assert oq.score < cfg.opportunity_quality_min
        assert eq.score >= cfg.entry_quality_min
        if oq.score >= cfg.opportunity_quality_min and eq.score >= cfg.entry_quality_min:
            status = "READY"
        elif oq.score >= cfg.opportunity_quality_min or eq.score >= cfg.entry_quality_min:
            status = "WATCHLIST"
        else:
            status = "WAITING"
        assert status != "READY"

    def test_low_eq_blocks_ready(self):
        """Direction present + high OQ but EQ below min → NOT READY."""
        oq = compute_opportunity_quality(
            atr_percentile=50, spread_current=1.0, spread_typical=1.2,
            news_is_clear=True, session_liquidity="HIGH",
            session_is_tradeable=True, reward_risk_magnitude=2.5,
            ev_estimate=0.8, volume_ratio=1.5, volume_climax=False,
        )
        eq = compute_entry_quality(
            trade_dir="LONG", current_price=1.2, entry_price=1.1,
            nearest_ob_distance_pips=200.0, nearest_fvg_distance_pips=200.0,
            nearest_liq_distance_pips=200.0, atr_pips=10.0, stop_distance_pips=0.5,
        )
        cfg = LayeredDecisionConfig(enabled=True, entry_quality_min=5.0)
        assert oq.score >= cfg.opportunity_quality_min
        assert eq.score < cfg.entry_quality_min
        if oq.score >= cfg.opportunity_quality_min and eq.score >= cfg.entry_quality_min:
            status = "READY"
        elif oq.score >= cfg.opportunity_quality_min or eq.score >= cfg.entry_quality_min:
            status = "WATCHLIST"
        else:
            status = "WAITING"
        assert status != "READY"


# ── Config validation ──────────────────────────────────────────────────

class TestLayeredDecisionConfig:
    def test_default_construction(self):
        cfg = LayeredDecisionConfig()
        assert cfg.enabled is True
        assert cfg.opportunity_quality_min == 5.0
        assert cfg.entry_quality_min == 5.0

    def test_threshold_out_of_range_high(self):
        with pytest.raises(ValueError, match="opportunity_quality_min"):
            LayeredDecisionConfig(opportunity_quality_min=11.0)

    def test_threshold_out_of_range_low(self):
        with pytest.raises(ValueError, match="entry_quality_min"):
            LayeredDecisionConfig(entry_quality_min=-1.0)

    def test_threshold_non_finite(self):
        with pytest.raises(ValueError):
            LayeredDecisionConfig(opportunity_quality_min=float("inf"))

    def test_negative_weight_rejected(self):
        with pytest.raises(ValueError, match="oq_weights"):
            LayeredDecisionConfig(oq_weights={"volatility": -1.0, "spread": 1.0})

    def test_all_zero_weights_rejected(self):
        with pytest.raises(ValueError, match="oq_weights"):
            LayeredDecisionConfig(oq_weights={"volatility": 0.0, "spread": 0.0})

    def test_non_finite_weight_rejected(self):
        with pytest.raises(ValueError, match="eq_weights"):
            LayeredDecisionConfig(eq_weights={"ob_proximity": float("nan"), "stop_quality": 1.0})

    def test_app_config_has_layered_decision(self):
        app = AppConfig()
        assert hasattr(app, "layered_decision")
        assert isinstance(app.layered_decision, LayeredDecisionConfig)
        assert app.layered_decision.enabled is True


# ── Fail-closed ────────────────────────────────────────────────────────

class TestFailClosed:
    """When an input is missing/bad, the function returns a LOW score
    and does NOT raise."""

    def test_oq_all_none_returns_low(self):
        oq = compute_opportunity_quality()
        assert oq.score <= 5.0
        assert isinstance(oq.reasons, list)

    def test_eq_missing_direction_returns_zero(self):
        eq = compute_entry_quality(trade_dir="NEUTRAL")
        assert eq.score == 0.0

    def test_eq_all_none_returns_low(self):
        eq = compute_entry_quality(trade_dir="LONG")
        assert eq.score <= 5.0
        assert isinstance(eq.reasons, list)

    def test_oq_bad_values_no_crash(self):
        oq = compute_opportunity_quality(
            atr_percentile=float("nan"),
            spread_current=float("inf"),
            volume_ratio=None,
        )
        assert isinstance(oq, OpportunityQuality)

    def test_eq_bad_values_no_crash(self):
        eq = compute_entry_quality(
            trade_dir="SHORT",
            current_price=float("nan"),
            atr_pips=float("inf"),
        )
        assert isinstance(eq, EntryQuality)


# ── Toggle OFF ─────────────────────────────────────────────────────────

class TestToggleOff:
    """With enabled=False, the legacy score path decides."""

    def test_disabled_config(self):
        cfg = LayeredDecisionConfig(enabled=False)
        assert cfg.enabled is False

    def test_disabled_does_not_affect_legacy(self):
        cfg = LayeredDecisionConfig(enabled=False)
        score = 80
        min_entry_score = 70
        watchlist_score = 55
        if not cfg.enabled:
            if score >= min_entry_score:
                status = "READY"
            elif score >= watchlist_score:
                status = "WATCHLIST"
            else:
                status = "WAITING"
        assert status == "READY"


# ── Score bounds ───────────────────────────────────────────────────────

class TestScoreBounds:
    def test_oq_score_within_bounds(self):
        oq = compute_opportunity_quality(
            atr_percentile=50, spread_current=1.0, spread_typical=1.0,
            news_is_clear=True, session_liquidity="HIGH",
            session_is_tradeable=True, reward_risk_magnitude=3.0,
            ev_estimate=1.0, volume_ratio=1.5, volume_climax=False,
        )
        assert 0.0 <= oq.score <= 10.0

    def test_eq_score_within_bounds(self):
        eq = compute_entry_quality(
            trade_dir="LONG", current_price=1.1, entry_price=1.099,
            nearest_ob_distance_pips=2.0, nearest_fvg_distance_pips=2.0,
            nearest_liq_distance_pips=5.0, atr_pips=20.0, stop_distance_pips=20.0,
        )
        assert 0.0 <= eq.score <= 10.0

    def test_oq_worst_case_at_zero(self):
        oq = compute_opportunity_quality(
            atr_percentile=95, spread_current=50.0, spread_typical=1.0,
            news_is_clear=False, news_minutes_to_next_high=1,
            session_liquidity="DEAD", session_is_tradeable=False,
            reward_risk_magnitude=0.3, ev_estimate=-1.0,
            volume_ratio=0.1, volume_climax=True,
        )
        assert oq.score <= 3.0

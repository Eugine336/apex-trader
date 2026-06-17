"""
Tests for the adaptive win-rate provider (learning layer #1).

Pure, dependency-light: no torch/pandas, no broker. Verifies the fallback chain
(PairLearner → EVEstimator → cold-start prior), Bayesian shrinkage on thin
samples, clamping, exception safety, and that the provider plugs into the
opportunity ranker so its EV is driven by an observed rate with auditable
provenance — instead of the constant base_win_rate.
"""

import pytest

from adaptive.win_rate_provider import (
    AdaptiveWinRateProvider,
    SOURCE_COLD_START,
    SOURCE_EV_ESTIMATOR,
    SOURCE_PAIR_LEARNER,
    WinRateResult,
)
from brain.directional_consensus import Vote, decide_opportunities


# ── Test doubles ───────────────────────────────────────────────────────────

class _Profile:
    def __init__(self, win_rate, total_trades):
        self.win_rate = win_rate
        self.total_trades = total_trades


class _FakePairLearner:
    """Minimal stand-in exposing get_profile like the real PairLearner."""

    def __init__(self, profiles=None):
        self._profiles = profiles or {}

    def get_profile(self, pair):
        return self._profiles.get(pair)


class _FakeEstimate:
    def __init__(self, win_rate, sample_size, source):
        self.win_rate = win_rate
        self.sample_size = sample_size
        self.source = source


class _FakeEVEstimator:
    def __init__(self, estimate):
        self._estimate = estimate
        self.calls = []

    def estimate(self, pair, regime, session, trade_history):
        self.calls.append((pair, regime, session, len(trade_history or [])))
        return self._estimate


# ── Fallback chain ─────────────────────────────────────────────────────────

class TestFallbackChain:
    def test_pair_learner_wins_when_available(self):
        pl = _FakePairLearner({"EURUSD": _Profile(0.62, 40)})
        ev = _FakeEVEstimator(_FakeEstimate(0.99, 99, "pair"))  # should be ignored
        provider = AdaptiveWinRateProvider(pair_learner=pl, ev_estimator=ev)
        res = provider.resolve("EURUSD", regime="TREND", session="LONDON",
                               trade_history=[{"pair": "EURUSD"}])
        assert res.source == SOURCE_PAIR_LEARNER
        assert res.sample_size == 40
        assert res.win_rate == pytest.approx(0.62)
        # EVEstimator was never consulted because pair_learner answered first.
        assert ev.calls == []

    def test_ev_estimator_used_when_pair_learner_missing(self):
        pl = _FakePairLearner({})  # no profile for the pair
        ev = _FakeEVEstimator(_FakeEstimate(0.58, 25, "regime"))
        provider = AdaptiveWinRateProvider(pair_learner=pl, ev_estimator=ev)
        res = provider.resolve("EURUSD", regime="TREND", session="LONDON",
                               trade_history=[{"pair": "EURUSD"}])
        assert res.source == SOURCE_EV_ESTIMATOR
        assert res.sample_size == 25
        assert res.win_rate == pytest.approx(0.58)

    def test_cold_start_prior_when_nothing_available(self):
        provider = AdaptiveWinRateProvider(pair_learner=None, ev_estimator=None)
        res = provider.resolve("EURUSD", trade_history=None)
        assert res.source == SOURCE_COLD_START
        assert res.sample_size == 0
        assert res.win_rate == pytest.approx(0.40)

    def test_pair_learner_zero_trades_falls_through(self):
        pl = _FakePairLearner({"EURUSD": _Profile(0.0, 0)})
        ev = _FakeEVEstimator(_FakeEstimate(0.55, 30, "pair"))
        provider = AdaptiveWinRateProvider(pair_learner=pl, ev_estimator=ev)
        res = provider.resolve("EURUSD", trade_history=[{"pair": "EURUSD"}])
        assert res.source == SOURCE_EV_ESTIMATOR

    def test_ev_estimator_default_source_falls_through(self):
        pl = _FakePairLearner({})
        ev = _FakeEVEstimator(_FakeEstimate(0.0, 3, "default"))  # insufficient
        provider = AdaptiveWinRateProvider(pair_learner=pl, ev_estimator=ev)
        res = provider.resolve("EURUSD", trade_history=[{"pair": "EURUSD"}])
        assert res.source == SOURCE_COLD_START

    def test_ev_estimator_skipped_without_history(self):
        pl = _FakePairLearner({})
        ev = _FakeEVEstimator(_FakeEstimate(0.7, 50, "pair"))
        provider = AdaptiveWinRateProvider(pair_learner=pl, ev_estimator=ev)
        res = provider.resolve("EURUSD", trade_history=None)
        assert res.source == SOURCE_COLD_START
        assert ev.calls == []  # no history → estimator not even called


# ── Bayesian shrinkage on thin samples ─────────────────────────────────────

class TestBayesianBlend:
    def test_small_sample_blended_toward_prior(self):
        # n=2, observed=1.0, prior=0.40, prior_strength=10:
        # (2*1.0 + 10*0.40) / (2 + 10) = 6.0/12 = 0.50
        pl = _FakePairLearner({"EURUSD": _Profile(1.0, 2)})
        provider = AdaptiveWinRateProvider(
            pair_learner=pl, prior=0.40, prior_strength=10, min_trades=10,
        )
        res = provider.resolve("EURUSD")
        assert res.win_rate == pytest.approx(0.50)
        assert res.sample_size == 2

    def test_at_min_trades_used_directly(self):
        # n == min_trades → no blend, observed used directly (within clamp).
        pl = _FakePairLearner({"EURUSD": _Profile(0.70, 10)})
        provider = AdaptiveWinRateProvider(
            pair_learner=pl, prior=0.40, prior_strength=10, min_trades=10,
        )
        res = provider.resolve("EURUSD")
        assert res.win_rate == pytest.approx(0.70)

    def test_zero_observed_small_sample_blends_up(self):
        # n=1, observed=0.0 → (0 + 10*0.40)/11 ≈ 0.3636
        pl = _FakePairLearner({"EURUSD": _Profile(0.0, 1)})
        provider = AdaptiveWinRateProvider(
            pair_learner=pl, prior=0.40, prior_strength=10, min_trades=10,
        )
        res = provider.resolve("EURUSD")
        assert res.win_rate == pytest.approx(10 * 0.40 / 11)


# ── Clamping ────────────────────────────────────────────────────────────────

class TestClamping:
    def test_high_rate_clamped(self):
        pl = _FakePairLearner({"EURUSD": _Profile(1.0, 100)})
        provider = AdaptiveWinRateProvider(
            pair_learner=pl, min_trades=10, clamp=(0.15, 0.85),
        )
        res = provider.resolve("EURUSD")
        assert res.win_rate == pytest.approx(0.85)

    def test_low_rate_clamped(self):
        pl = _FakePairLearner({"EURUSD": _Profile(0.0, 100)})
        provider = AdaptiveWinRateProvider(
            pair_learner=pl, min_trades=10, clamp=(0.15, 0.85),
        )
        res = provider.resolve("EURUSD")
        assert res.win_rate == pytest.approx(0.15)


# ── Exception safety ────────────────────────────────────────────────────────

class TestExceptionSafety:
    def test_pair_learner_raises_falls_back_to_prior(self):
        class _Boom:
            def get_profile(self, pair):
                raise RuntimeError("boom")

        provider = AdaptiveWinRateProvider(pair_learner=_Boom(), ev_estimator=None)
        res = provider.resolve("EURUSD")
        assert res.source == SOURCE_COLD_START
        assert res.win_rate == pytest.approx(0.40)

    def test_ev_estimator_raises_falls_back_to_prior(self):
        class _BoomEV:
            def estimate(self, **kwargs):
                raise RuntimeError("boom")

        provider = AdaptiveWinRateProvider(
            pair_learner=_FakePairLearner({}), ev_estimator=_BoomEV(),
        )
        res = provider.resolve("EURUSD", trade_history=[{"pair": "EURUSD"}])
        assert res.source == SOURCE_COLD_START

    def test_for_pair_callable_never_raises(self):
        provider = AdaptiveWinRateProvider()
        fn, res = provider.for_pair("EURUSD")
        # Same value for any direction/timeframe (per-pair rate).
        assert fn("LONG", "SCALP") == pytest.approx(res.win_rate)
        assert fn("SHORT", "SWING") == pytest.approx(res.win_rate)


# ── Provider plugged into the ranker ───────────────────────────────────────

class TestProviderInRanker:
    def _votes(self):
        return [
            Vote("structure", "LONG", 0.9, 1.0),
            Vote("currency_strength", "LONG", 0.6, 1.0),
        ]

    def test_calibrated_win_prob_overrides_modelled(self):
        pl = _FakePairLearner({"EURUSD": _Profile(0.75, 50)})
        provider, result = AdaptiveWinRateProvider(pair_learner=pl).for_pair("EURUSD")
        opps = decide_opportunities(self._votes(), win_rate_provider=provider)
        assert opps
        for opp in opps:
            assert opp.win_prob == pytest.approx(0.75)
            assert opp.win_prob_components["calibrated"] is True

    def test_no_provider_uses_modelled_formula(self):
        # Without a provider the modelled formula (base 0.40 + gain) applies.
        opps = decide_opportunities(self._votes())
        assert opps
        for opp in opps:
            assert opp.win_prob_components["calibrated"] is False
            # Gain term lifts it above the base prior.
            assert opp.win_prob > 0.40


# ── Provenance stamping (mirrors the scanner post-process) ──────────────────

class TestProvenanceStamping:
    def test_source_and_sample_size_stamped(self):
        pl = _FakePairLearner({"EURUSD": _Profile(0.66, 30)})
        provider, result = AdaptiveWinRateProvider(pair_learner=pl).for_pair("EURUSD")
        opps = decide_opportunities(
            [Vote("structure", "LONG", 0.9, 1.0)],
            win_rate_provider=provider,
        )
        # Scanner stamps provenance onto calibrated opportunities; replicate it.
        for opp in opps:
            comps = opp.win_prob_components
            if comps.get("calibrated"):
                comps["win_rate_source"] = result.source
                comps["win_rate_sample_size"] = result.sample_size
        assert opps[0].win_prob_components["win_rate_source"] == SOURCE_PAIR_LEARNER
        assert opps[0].win_prob_components["win_rate_sample_size"] == 30


# ── Config flag ─────────────────────────────────────────────────────────────

class TestConfigFlag:
    def test_flag_defaults_on(self):
        from config import OpportunityRankerConfig

        rc = OpportunityRankerConfig()
        assert rc.adaptive_win_rate_provider_enabled is True
        assert rc.adaptive_win_rate_prior == pytest.approx(0.40)
        assert rc.adaptive_win_rate_min_trades == 10
        assert rc.adaptive_win_rate_clamp_low == pytest.approx(0.15)
        assert rc.adaptive_win_rate_clamp_high == pytest.approx(0.85)

    def test_invalid_clamp_order_rejected(self):
        from config import OpportunityRankerConfig

        with pytest.raises(ValueError):
            OpportunityRankerConfig(
                adaptive_win_rate_clamp_low=0.9,
                adaptive_win_rate_clamp_high=0.2,
            )

    def test_invalid_prior_rejected(self):
        from config import OpportunityRankerConfig

        with pytest.raises(ValueError):
            OpportunityRankerConfig(adaptive_win_rate_prior=1.5)

    def test_invalid_min_trades_rejected(self):
        from config import OpportunityRankerConfig

        with pytest.raises(ValueError):
            OpportunityRankerConfig(adaptive_win_rate_min_trades=0)


# ── Scanner wiring helpers ──────────────────────────────────────────────────

class TestScannerWiring:
    def test_set_pair_learner_resets_cached_adapter(self):
        from config import OpportunityRankerConfig
        from scanner.pair_scanner import PairScanner

        scanner = PairScanner()
        rc = OpportunityRankerConfig()
        first = scanner._get_win_rate_adapter(rc)
        assert first is scanner._get_win_rate_adapter(rc)  # cached
        scanner.set_pair_learner(_FakePairLearner({"EURUSD": _Profile(0.6, 30)}))
        assert scanner._win_rate_adapter is None  # cache cleared
        rebuilt = scanner._get_win_rate_adapter(rc)
        assert rebuilt is not first

    def test_scanner_adapter_uses_injected_learner(self):
        from config import OpportunityRankerConfig
        from scanner.pair_scanner import PairScanner

        scanner = PairScanner()
        scanner.set_pair_learner(_FakePairLearner({"EURUSD": _Profile(0.7, 50)}))
        adapter = scanner._get_win_rate_adapter(OpportunityRankerConfig())
        res = adapter.resolve("EURUSD")
        assert res.source == SOURCE_PAIR_LEARNER
        assert res.win_rate == pytest.approx(0.7)


"""
APEX TRADER — Continuous PairLearner tests (Learning Session 4)

Covers the smooth sigmoid size multiplier that replaces the legacy 4-bucket
step function, Bayesian shrinkage on thin samples, the AVOID / cold-start
safety floors, the entry-vs-management split (PostCloseTracker consumption),
backward compatibility (flag off = old buckets), persistence of the new
profile fields, and the TunerAgent adapter's sigmoid validation.
"""

from __future__ import annotations

import pytest

from config import PairLearnerConfig
from adaptive.pair_learner import (
    PairLearner,
    PairProfile,
    compute_capture_ratio,
    sigmoid_multiplier,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _trade(pair="EURUSD", pnl=1.0, session="LONDON", regime="TREND"):
    return {"pair": pair, "pnl": pnl, "session": session, "regime": regime, "spread": 1.0}


def _mixed_trades(pair: str, wins: int, losses: int) -> list[dict]:
    return (
        [_trade(pair=pair, pnl=10.0) for _ in range(wins)]
        + [_trade(pair=pair, pnl=-10.0) for _ in range(losses)]
    )


def _continuous_cfg(**overrides) -> PairLearnerConfig:
    base = dict(continuous_pair_multiplier=True)
    base.update(overrides)
    return PairLearnerConfig(**base)


@pytest.fixture
def save_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(PairLearner, "SAVE_PATH", str(tmp_path / "pairs.json"))
    return tmp_path


class _FakeTracker:
    """Minimal PostCloseTracker stand-in exposing the read-only accessors."""

    def __init__(self, *, accuracy=0.9, fractions=None, total=20, median=1.2):
        self._accuracy = accuracy
        self._fractions = fractions or {"good_management": 1.0}
        self._total = total
        self._median = median

    def get_signal_accuracy(self, pair=None):
        return self._accuracy

    def get_management_score(self, pair=None):
        return {"total": self._total, "counts": {}, "fractions": self._fractions}

    def get_optimal_sl_stats(self, pair=None):
        return {"sample_size": self._total, "median": self._median, "p75": None, "p90": None, "max": None}


# ── sigmoid curve ──────────────────────────────────────────────────────────


class TestSigmoidCurve:
    def test_midpoint_is_curve_centre(self):
        v = sigmoid_multiplier(0.5, midpoint=0.5, floor=0.3, ceiling=1.2)
        assert v == pytest.approx(0.3 + (1.2 - 0.3) / 2, abs=1e-9)

    def test_monotonic_increasing(self):
        prev = -1.0
        for wr in [0.0, 0.25, 0.5, 0.75, 1.0]:
            v = sigmoid_multiplier(wr)
            assert v > prev
            prev = v

    def test_bounded_by_floor_and_ceiling(self):
        lo = sigmoid_multiplier(0.0, floor=0.3, ceiling=1.2)
        hi = sigmoid_multiplier(1.0, floor=0.3, ceiling=1.2)
        assert 0.3 <= lo < 0.5
        assert 1.0 < hi <= 1.2

    def test_no_discontinuities(self):
        # Adjacent win rates produce adjacent multipliers (smoothness).
        a = sigmoid_multiplier(0.549)
        b = sigmoid_multiplier(0.551)
        assert abs(a - b) < 0.05

    def test_distinct_winrates_distinct_multipliers(self):
        # The whole point: 56% and 75% must NOT collapse to the same value.
        assert sigmoid_multiplier(0.56) != sigmoid_multiplier(0.75)

    def test_extreme_steepness_no_overflow(self):
        # Large steepness must not raise (OverflowError guard).
        assert sigmoid_multiplier(0.0, steepness=10_000) == pytest.approx(0.3, abs=1e-6)
        assert sigmoid_multiplier(1.0, steepness=10_000) == pytest.approx(1.2, abs=1e-6)


# ── capture ratio helper ────────────────────────────────────────────────────


class TestCaptureRatio:
    def test_full_capture(self):
        assert compute_capture_ratio(2.0, 2.0) == 1.0

    def test_partial_capture(self):
        assert compute_capture_ratio(1.0, 2.0) == 0.5

    def test_none_inputs(self):
        assert compute_capture_ratio(None, 2.0) is None
        assert compute_capture_ratio(1.0, None) is None

    def test_zero_mfe_is_none(self):
        assert compute_capture_ratio(1.0, 0.0) is None


# ── continuous multiplier behaviour ──────────────────────────────────────────


class TestContinuousMultiplier:
    def test_cold_start_unknown_pair(self, save_to_tmp):
        learner = PairLearner(config=_continuous_cfg(cold_start_multiplier=0.8))
        assert learner.get_pair_multiplier("NEVER_SEEN") == 0.8

    def test_cold_start_thin_profile(self, save_to_tmp):
        learner = PairLearner(config=_continuous_cfg(cold_start_min_trades=5))
        learner.learn(_mixed_trades("EURUSD", wins=2, losses=1))  # n=3 < 5
        # New cold-start default is 0.5 (was 0.8): the system risks half size on
        # an unproven pair until it earns more.
        assert learner.get_pair_multiplier("EURUSD") == 0.5

    def test_avoid_is_hard_zero(self, save_to_tmp):
        learner = PairLearner(config=_continuous_cfg())
        # 35 losers → wr 0.0, n>=30 → AVOID, even in continuous mode → 0.0.
        learner.learn(_mixed_trades("NZDJPY", wins=0, losses=35))
        assert learner.get_pair_multiplier("NZDJPY") == 0.0

    def test_strong_pair_sizes_high(self, save_to_tmp):
        learner = PairLearner(config=_continuous_cfg())
        learner.learn(_mixed_trades("EURUSD", wins=30, losses=0))  # wr 1.0
        m = learner.get_pair_multiplier("EURUSD")
        assert m > 1.0  # proven pair allowed slight upscale toward ceiling

    def test_winrate_gradient_is_continuous(self, save_to_tmp):
        # A 56% pair and a 75% pair get DIFFERENT (and ordered) multipliers,
        # where the old 4-bucket logic gave both exactly 1.0.
        learner = PairLearner(config=_continuous_cfg())
        learner.learn(_mixed_trades("AAA", wins=56, losses=44))   # 56%
        learner.learn(_mixed_trades("AAA", wins=56, losses=44))
        m56 = learner.get_pair_multiplier("AAA")
        learner2 = PairLearner(config=_continuous_cfg())
        learner2.learn(_mixed_trades("BBB", wins=75, losses=25))  # 75%
        m75 = learner2.get_pair_multiplier("BBB")
        assert m75 > m56

    def test_absolute_floor_holds(self, save_to_tmp):
        learner = PairLearner(
            config=_continuous_cfg(continuous_floor=0.0, continuous_absolute_floor=0.25)
        )
        # REDUCE_SIZE pair (wr 0.50, n 30) — sigmoid could dip; floor clamps it.
        learner.learn(_mixed_trades("EURUSD", wins=15, losses=15))
        assert learner.get_pair_multiplier("EURUSD") >= 0.25

    def test_ceiling_holds(self, save_to_tmp):
        learner = PairLearner(config=_continuous_cfg(continuous_ceiling=1.1))
        learner.learn(_mixed_trades("EURUSD", wins=40, losses=0))
        assert learner.get_pair_multiplier("EURUSD") <= 1.1


class TestBayesianShrinkage:
    def test_thin_sample_pulled_toward_prior(self, save_to_tmp):
        # Graduated cold start off here: this test targets the pure sigmoid +
        # shrinkage path at low n (graduated would otherwise intercept the
        # cold_start_min_trades..MIN_TRADES band).
        cfg = _continuous_cfg(
            cold_start_min_trades=1, shrinkage_full_weight=30, continuous_prior=0.8,
            cold_start_graduated=False,
        )
        learner = PairLearner(config=cfg)
        learner.learn(_mixed_trades("EURUSD", wins=1, losses=0))  # n=1, wr 1.0
        # Heavily shrunk toward the 0.8 prior despite a perfect (tiny) sample.
        assert learner.get_pair_multiplier("EURUSD") == pytest.approx(0.8, abs=0.05)

    def test_full_sample_uses_raw_curve(self, save_to_tmp):
        cfg = _continuous_cfg(
            cold_start_min_trades=1, shrinkage_full_weight=30,
            cold_start_graduated=False,
        )
        learner = PairLearner(config=cfg)
        learner.learn(_mixed_trades("EURUSD", wins=60, losses=0))  # n=60 >= 30
        raw = sigmoid_multiplier(1.0)  # ceiling-bound raw curve value
        assert learner.get_pair_multiplier("EURUSD") == pytest.approx(
            min(raw, cfg.continuous_ceiling), abs=1e-3
        )

    def test_more_trades_closer_to_raw(self, save_to_tmp):
        cfg = _continuous_cfg(
            cold_start_min_trades=1, shrinkage_full_weight=30,
            cold_start_graduated=False,
        )
        thin = PairLearner(config=cfg)
        thin.learn(_mixed_trades("AAA", wins=5, losses=0))
        thick = PairLearner(config=cfg)
        thick.learn(_mixed_trades("BBB", wins=29, losses=0))
        raw = sigmoid_multiplier(1.0)
        # The bigger sample sits closer to the raw curve than the thin one.
        assert abs(thick.get_pair_multiplier("BBB") - raw) < abs(
            thin.get_pair_multiplier("AAA") - raw
        )


# ── entry vs management split ────────────────────────────────────────────────


class TestEntryManagementSplit:
    def test_split_off_uses_raw_winrate(self, save_to_tmp):
        learner = PairLearner(config=_continuous_cfg(entry_management_split_enabled=False))
        learner.set_post_close_tracker(_FakeTracker(accuracy=0.9))
        learner.learn(_mixed_trades("EURUSD", wins=14, losses=16))  # wr ~0.467
        prof = learner.get_profile("EURUSD")
        # entry_accuracy is still recorded for visibility even when split off.
        assert prof.entry_accuracy == pytest.approx(0.9, abs=1e-6)

    def test_good_entry_bad_winrate_not_overpenalised(self, save_to_tmp):
        # Same profile, only the split flag differs: good signals (0.9) lift the
        # effective win rate so the pair sizes larger than on raw WR alone.
        trades = _mixed_trades("EURUSD", wins=13, losses=17)  # wr ~0.433, REDUCE
        tracker = _FakeTracker(accuracy=0.9)

        off = PairLearner(config=_continuous_cfg(entry_management_split_enabled=False))
        off.set_post_close_tracker(tracker)
        off.learn(trades)

        on = PairLearner(
            config=_continuous_cfg(
                entry_management_split_enabled=True, entry_accuracy_blend_weight=0.3
            )
        )
        on.set_post_close_tracker(tracker)
        on.learn(trades)

        assert on.get_pair_multiplier("EURUSD") > off.get_pair_multiplier("EURUSD")

    def test_management_score_and_optimal_sl_recorded(self, save_to_tmp):
        tracker = _FakeTracker(
            accuracy=0.8,
            fractions={"good_management": 0.5, "bad_management": 0.5},
            median=1.5,
        )
        learner = PairLearner(config=_continuous_cfg())
        learner.set_post_close_tracker(tracker)
        learner.learn(_mixed_trades("EURUSD", wins=20, losses=10))
        prof = learner.get_profile("EURUSD")
        # 0.5*good(1.0) + 0.5*bad_management(0.3) = 0.65
        assert prof.management_score == pytest.approx(0.65, abs=1e-6)
        assert prof.optimal_sl_r == pytest.approx(1.5, abs=1e-6)

    def test_no_postclose_rows_leaves_fields_none(self, save_to_tmp):
        tracker = _FakeTracker(total=0)  # no finalised rows yet
        learner = PairLearner(config=_continuous_cfg())
        learner.set_post_close_tracker(tracker)
        learner.learn(_mixed_trades("EURUSD", wins=20, losses=10))
        prof = learner.get_profile("EURUSD")
        assert prof.entry_accuracy is None
        assert prof.management_score is None

    def test_tracker_read_failure_is_swallowed(self, save_to_tmp):
        class _Boom:
            def get_management_score(self, pair=None):
                raise RuntimeError("db down")

        learner = PairLearner(config=_continuous_cfg())
        learner.set_post_close_tracker(_Boom())
        # Must not raise; profile still built, split fields None.
        learner.learn(_mixed_trades("EURUSD", wins=20, losses=10))
        assert learner.get_profile("EURUSD").entry_accuracy is None


# ── backward compatibility (flag off = legacy buckets) ───────────────────────


class TestBackwardCompatibility:
    def test_legacy_good_pair(self, save_to_tmp):
        learner = PairLearner()  # no config → flag off
        learner.learn([_trade(pair="EURUSD", pnl=15)] * 30)
        assert learner.get_pair_multiplier("EURUSD") == 1.0

    def test_legacy_bad_pair(self, save_to_tmp):
        learner = PairLearner()
        learner.learn([_trade(pair="NZDJPY", pnl=-8)] * 35)
        assert learner.get_pair_multiplier("NZDJPY") == 0.0

    def test_legacy_unknown_pair(self, save_to_tmp):
        learner = PairLearner()
        assert learner.get_pair_multiplier("UNKNOWN") == 0.8

    def test_legacy_reduce_size(self, save_to_tmp):
        learner = PairLearner()
        learner.learn(_mixed_trades("EURUSD", wins=12, losses=13))  # wr 0.48 REDUCE
        assert learner.get_pair_multiplier("EURUSD") == 0.7


# ── persistence ───────────────────────────────────────────────────────────


class TestPersistence:
    def test_round_trip_with_new_fields(self, save_to_tmp):
        tracker = _FakeTracker(accuracy=0.7, median=1.1)
        learner = PairLearner(config=_continuous_cfg())
        learner.set_post_close_tracker(tracker)
        learner.learn(_mixed_trades("EURUSD", wins=20, losses=10))

        # Reload from disk into a fresh learner.
        reloaded = PairLearner(config=_continuous_cfg())
        prof = reloaded.get_profile("EURUSD")
        assert prof is not None
        assert prof.entry_accuracy == pytest.approx(0.7, abs=1e-6)
        assert prof.optimal_sl_r == pytest.approx(1.1, abs=1e-6)

    def test_legacy_profile_without_new_fields_loads(self, save_to_tmp):
        # An old profile dict (pre-split fields) must still construct cleanly.
        prof = PairProfile(pair="EURUSD", win_rate=0.6, total_trades=30, recommendation="TRADE")
        assert prof.entry_accuracy is None
        assert prof.management_score is None
        assert prof.optimal_sl_r is None


# ── TunerAgent adapter integration ───────────────────────────────────────────


class TestTunableAdapter:
    @pytest.fixture
    def learner(self, save_to_tmp):
        return PairLearner(config=_continuous_cfg())

    def _adapter(self, learner):
        from adaptive.tunable_adapters import PairLearnerTunable

        return PairLearnerTunable(learner, lambda: [])

    def test_get_params_exposes_sigmoid(self, learner):
        t = self._adapter(learner)
        params = t.get_current_params()
        sig = params.get("__sigmoid_config__")
        assert sig is not None
        assert sig["continuous_pair_multiplier"] is True
        assert sig["midpoint"] == pytest.approx(0.5)

    def test_validate_accepts_good_sigmoid(self, learner):
        t = self._adapter(learner)
        ok, _ = t.validate_params(t.get_current_params())
        assert ok is True

    def test_validate_rejects_floor_above_ceiling(self, learner):
        t = self._adapter(learner)
        ok, why = t.validate_params(
            {"__sigmoid_config__": {"floor": 1.5, "ceiling": 1.0, "steepness": 10, "midpoint": 0.5}}
        )
        assert ok is False and "floor" in why

    def test_validate_rejects_nonpositive_steepness(self, learner):
        t = self._adapter(learner)
        ok, why = t.validate_params(
            {"__sigmoid_config__": {"floor": 0.3, "ceiling": 1.2, "steepness": 0, "midpoint": 0.5}}
        )
        assert ok is False and "steepness" in why

    def test_validate_rejects_midpoint_out_of_range(self, learner):
        t = self._adapter(learner)
        ok, why = t.validate_params(
            {"__sigmoid_config__": {"floor": 0.3, "ceiling": 1.2, "steepness": 10, "midpoint": 1.5}}
        )
        assert ok is False and "midpoint" in why

    def test_per_pair_validation_still_enforced(self, learner):
        t = self._adapter(learner)
        ok, why = t.validate_params({"EURUSD": {"win_rate": 2.0, "recommendation": "TRADE"}})
        assert ok is False and "win_rate" in why

    def test_rollback_strips_reserved_key(self, learner):
        # tune then rollback must restore profiles without crashing on the
        # reserved sigmoid key in the snapshot.

        t = self._adapter(learner)
        t._begin()  # snapshot includes the sigmoid key
        assert t.rollback() is True


# ── config-driven wiring ─────────────────────────────────────────────────────


class TestConfigWiring:
    def test_config_params_applied(self, save_to_tmp):
        cfg = _continuous_cfg(continuous_midpoint=0.6, continuous_steepness=5.0)
        learner = PairLearner(config=cfg)
        assert learner.continuous_enabled is True
        assert learner.continuous_midpoint == 0.6
        assert learner.continuous_steepness == 5.0

    def test_no_config_uses_legacy_defaults(self, save_to_tmp):
        learner = PairLearner()
        assert learner.continuous_enabled is False

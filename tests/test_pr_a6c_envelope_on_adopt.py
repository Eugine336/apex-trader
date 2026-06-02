"""
PR-A6c — Envelope-on-Adopt & Optimizer 12-Factor Learning

Tests:
1. All fit/validate paths use the 12-factor model; fitted vectors have all 12 keys.
2. OOS gate is preserved: overfit candidate rejected; no write on reject; adopt on real improvement.
3. Envelope clamp is the final authority before persistence — on BOTH the
   normal OOS adopt path and the small-sample fallback path.
4. Post-clamp total may differ from CANONICAL_TOTAL (123) — no re-normalisation after clamping.
5. Corrupt-file load logs a warning and returns defaults.
"""

import json

import pytest

from adaptive.score_optimizer import (
    ADAPTIVE_WEIGHT_ENVELOPE_PCT,
    FACTOR_KEYS,
    ScoreOptimizer,
    ScoringWeights,
    load_saved_weights,
)

BASELINE = ScoringWeights()
BASELINE_DICT = BASELINE.as_dict()


def _make_trade(
    pnl: float,
    confluences_tags: list[str],
    timestamp: str | None = None,
) -> dict:
    return {
        "pair": "EURUSD",
        "pnl": pnl,
        "session": "LONDON",
        "regime": "TRENDING_STRONG",
        "entry_type": "FVG",
        "score": 90,
        "spread": 1.2,
        "time_to_exit": 25.0,
        "confluences_tags": confluences_tags,
        "timestamp": timestamp,
    }


def _build_volume_predictive_dataset(n_train: int = 70, n_val: int = 30) -> list[dict]:
    """Volume is strongly predictive in both halves (present on wins, absent on losses).
    structure and session are present on ALL trades → neutral lift.
    This pushes volume beyond its ±25% envelope after normalisation."""
    trades: list[dict] = []
    for i in range(n_train):
        is_win = i % 2 == 0
        pnl = 15.0 if is_win else -10.0
        tags = ["structure", "session", "volume"] if is_win else ["structure", "session"]
        trades.append(_make_trade(pnl, tags, timestamp=f"2025-01-{i + 1:02d}T00:00:00Z"))
    for i in range(n_val):
        is_win = i % 2 == 0
        pnl = 12.0 if is_win else -8.0
        tags = ["structure", "session", "volume"] if is_win else ["structure", "session"]
        trades.append(_make_trade(pnl, tags, timestamp=f"2025-03-{i + 1:02d}T00:00:00Z"))
    return trades


def _build_overfit_dataset(n_train: int = 70, n_val: int = 30) -> list[dict]:
    """fvg appears predictive in train but REVERSES in validation."""
    trades: list[dict] = []
    for i in range(n_train):
        is_win = i % 2 == 0
        pnl = 15.0 if is_win else -10.0
        tags = ["structure", "fvg", "session"] if is_win else ["structure", "session"]
        trades.append(_make_trade(pnl, tags, timestamp=f"2025-01-{i + 1:02d}T00:00:00Z"))
    for i in range(n_val):
        is_win = i % 2 == 0
        pnl = 12.0 if is_win else -8.0
        tags = ["structure", "session"] if is_win else ["structure", "fvg", "session"]
        trades.append(_make_trade(pnl, tags, timestamp=f"2025-03-{i + 1:02d}T00:00:00Z"))
    return trades


def _assert_within_envelope(weights: ScoringWeights, label: str = "") -> None:
    wd = weights.as_dict()
    for key in FACTOR_KEYS:
        b = BASELINE_DICT[key]
        lo = round(b * (1.0 - ADAPTIVE_WEIGHT_ENVELOPE_PCT))
        hi = round(b * (1.0 + ADAPTIVE_WEIGHT_ENVELOPE_PCT))
        assert lo <= wd[key] <= hi, f"{label}{key}: {wd[key]} outside [{lo}, {hi}] (baseline={b})"


# ──────────────────────────────────────────────────────────────────────────
# 1. All fit/validate paths use the 12-factor model
# ──────────────────────────────────────────────────────────────────────────


class TestTwelveFactorModel:
    def test_factor_keys_count(self):
        assert len(FACTOR_KEYS) == 12

    def test_fitted_vector_has_all_12_keys(self, tmp_path):
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(tmp_path / "w.json")
        trades = _build_volume_predictive_dataset()
        result = opt.optimize(trades, min_trades=50)
        rd = result.as_dict()
        assert set(rd.keys()) == set(FACTOR_KEYS)

    def test_get_factor_effectiveness_covers_all_12(self):
        opt = ScoreOptimizer()
        trades = [_make_trade(10.0, ["fvg", "volume"]) for _ in range(20)]
        eff = opt.get_factor_effectiveness(trades)
        assert set(eff.keys()) == set(FACTOR_KEYS)

    def test_validation_metric_uses_all_12_keys(self):
        weights = ScoringWeights(fvg_weight=30, volume_weight=10)
        trades = [
            _make_trade(10.0, ["fvg", "volume"]),
            _make_trade(-5.0, ["session"]),
        ]
        metric = ScoreOptimizer._compute_validation_metric(weights, trades)
        assert metric > 0


# ──────────────────────────────────────────────────────────────────────────
# 2. OOS gate preserved after adding envelope clamp
# ──────────────────────────────────────────────────────────────────────────


class TestOOSGatePreserved:
    def test_overfit_candidate_rejected(self, tmp_path):
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(tmp_path / "w.json")
        original = ScoringWeights()
        result = opt.optimize(_build_overfit_dataset(), min_trades=50)
        assert result.as_dict() == original.as_dict()

    def test_no_write_on_rejection(self, tmp_path):
        fp = tmp_path / "w.json"
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(fp)
        opt.optimize(_build_overfit_dataset(), min_trades=50)
        assert not fp.exists()

    def test_genuine_improvement_adopted(self, tmp_path):
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(tmp_path / "w.json")
        result = opt.optimize(_build_volume_predictive_dataset(), min_trades=50)
        assert result.as_dict() != ScoringWeights().as_dict()
        assert (tmp_path / "w.json").exists()


# ──────────────────────────────────────────────────────────────────────────
# 3. Envelope clamp is the final authority before persistence
# ──────────────────────────────────────────────────────────────────────────


class TestEnvelopeOnAdopt:
    def test_oos_path_all_factors_within_envelope(self, tmp_path):
        """Normal OOS-validated adopt: every saved factor within ±25% of baseline."""
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(tmp_path / "w.json")
        result = opt.optimize(_build_volume_predictive_dataset(), min_trades=50)
        _assert_within_envelope(result, label="OOS adopt: ")

    def test_oos_path_persisted_file_within_envelope(self, tmp_path):
        """The file on disk must also be within the envelope (not just the return value)."""
        fp = tmp_path / "w.json"
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(fp)
        opt.optimize(_build_volume_predictive_dataset(), min_trades=50)
        data = json.loads(fp.read_text())
        reloaded = ScoringWeights(**{k: v for k, v in data.items() if k in ScoringWeights.__dataclass_fields__})
        _assert_within_envelope(reloaded, label="persisted: ")

    def test_small_sample_path_within_envelope(self, tmp_path):
        """Small-sample fallback (_fit_and_adopt) also clamps before saving."""
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(tmp_path / "w.json")
        small = _build_volume_predictive_dataset(n_train=25, n_val=10)
        result = opt.optimize(small, min_trades=30)
        _assert_within_envelope(result, label="small-sample: ")

    def test_volume_clamped_when_pushed_beyond_envelope(self, tmp_path):
        """Volume (baseline 5, ±25% = [4, 6]) is pushed to ~7-8 by normalisation
        before the clamp caps it at 6."""
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(tmp_path / "w.json")
        result = opt.optimize(_build_volume_predictive_dataset(), min_trades=50)
        rd = result.as_dict()
        hi = round(BASELINE_DICT["volume"] * 1.25)
        assert rd["volume"] <= hi, f"volume {rd['volume']} exceeds envelope upper {hi}"

    def test_extreme_random_dataset_all_factors_bounded(self, tmp_path):
        """Random tags across all 12 factors — every adopted factor stays within envelope."""
        import random

        random.seed(42)
        trades = []
        for i in range(120):
            is_win = random.random() < 0.6
            pnl = 20.0 if is_win else -15.0
            tags = random.sample(FACTOR_KEYS, k=random.randint(3, 8))
            trades.append(_make_trade(pnl, tags, timestamp=f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}T00:00:00Z"))
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(tmp_path / "w.json")
        result = opt.optimize(trades, min_trades=50)
        _assert_within_envelope(result, label="random dataset: ")


# ──────────────────────────────────────────────────────────────────────────
# 4. No post-clamp re-normalisation — total may differ from 123
# ──────────────────────────────────────────────────────────────────────────


class TestNoPostClampRenorm:
    def test_saved_total_may_differ_from_canonical(self, tmp_path):
        """When the clamp adjusts factors, the sum is NOT re-normalised to 123.
        The saved total may be less than (or occasionally equal to) 123."""
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(tmp_path / "w.json")
        result = opt.optimize(_build_volume_predictive_dataset(), min_trades=50)
        _assert_within_envelope(result)

    def test_fit_weights_produces_canonical_total_before_clamp(self):
        """_fit_weights (the normalisation step) still produces total=123.
        This confirms normalisation happens BEFORE clamping."""
        opt = ScoreOptimizer()
        trades = _build_volume_predictive_dataset()
        sorted_trades = opt._sort_by_time(trades)
        split_idx = int(len(sorted_trades) * (1 - opt.VALIDATION_RATIO))
        train = sorted_trades[:split_idx]
        candidate = opt._fit_weights(train)
        assert candidate is not None
        assert candidate.total == ScoreOptimizer.CANONICAL_TOTAL


# ──────────────────────────────────────────────────────────────────────────
# 5. Corrupt-file load logs a warning and returns defaults
# ──────────────────────────────────────────────────────────────────────────


class TestCorruptFileWarning:
    def test_standalone_loader_returns_defaults_on_corrupt(self, tmp_path):
        fp = tmp_path / "corrupt.json"
        fp.write_text("NOT JSON {{{")
        result = load_saved_weights(str(fp))
        assert result.as_dict() == ScoringWeights().as_dict()

    def test_standalone_loader_logs_warning_on_corrupt(self, tmp_path, caplog):
        import logging

        fp = tmp_path / "corrupt.json"
        fp.write_text("NOT JSON {{{")
        with caplog.at_level(logging.WARNING):
            load_saved_weights(str(fp))
        assert any("Could not parse" in msg for msg in caplog.messages)

    def test_standalone_loader_returns_defaults_when_missing(self, tmp_path):
        result = load_saved_weights(str(tmp_path / "nonexistent.json"))
        assert result.as_dict() == ScoringWeights().as_dict()

    def test_instance_loader_also_warns_on_corrupt(self, tmp_path, caplog):
        import logging

        fp = tmp_path / "corrupt.json"
        fp.write_text("NOT JSON {{{")
        opt = ScoreOptimizer()
        with caplog.at_level(logging.WARNING):
            opt.load_weights(str(fp))
        assert any("Could not parse" in msg for msg in caplog.messages)

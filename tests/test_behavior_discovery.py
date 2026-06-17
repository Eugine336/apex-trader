"""
Tests for the Behaviour Discovery engine (adaptive/behavior_discovery.py, L6)
and its TunerAgent adapter (adaptive.tunable_adapters.BehaviorDiscoveryTunable).

Covers feature extraction + normalisation, the distance metric, density
clustering (separation, noise floor, max-cluster cap), cluster scoring with
Bayesian shrinkage, the SHADOW → ACTIVE → RETIRED lifecycle (promote / retire /
stale / cooldown), centroid-matched behaviour continuity, recompute gating
(min-trades floor, interval, disabled), record-trade ingestion guards, SQLite
persistence round-trips, the dormant empty-state no-op, the dashboard state
shape, and Tunable-protocol compliance.

Deterministic: feeds fixed feature/R streams and uses temp SQLite DBs so nothing
touches the real data/ dir.
"""

import os
import tempfile

import pytest

from config import BehaviorDiscoveryConfig
from adaptive.behavior_discovery import (
    ACTIVE,
    RETIRED,
    SHADOW,
    BehaviorClusterer,
    BehaviorDiscoveryEngine,
    BehaviorRecord,
    BehaviorScorer,
    FeatureVector,
    TradeFeatureExtractor,
    feature_distance,
)
from adaptive.tunable import Tunable, TuneContext, TuneFrequency
from adaptive.tunable_adapters import BehaviorDiscoveryTunable


# ── Fixtures / helpers ──────────────────────────────────────────────────────


def _close(a, b, eps=1e-6):
    return abs(float(a) - float(b)) <= eps


@pytest.fixture()
def db_path():
    d = tempfile.mkdtemp()
    return os.path.join(d, "behavior_discovery.db")


def _make(db_path, **overrides):
    kwargs = dict(
        db_path=db_path,
        enabled=True,
        min_trades_to_cluster=40,
        min_cluster_size=10,
        recluster_every_n_trades=10,
        cluster_eps=0.2,
        cooldown_trades=0,
    )
    kwargs.update(overrides)
    return BehaviorDiscoveryEngine(**kwargs)


_SCALP = {
    "entry_mode": "MARKET", "horizon": "SCALP", "profile": "tight_scalp",
    "direction": "LONG", "regime": "TREND", "sl_atr_mult": 1.0, "tp1_rr": 1.5,
    "tp2_rr": 2.0, "consensus_strength": 0.8, "conviction": 0.7, "score": 80,
}
_SWING = {
    "entry_mode": "PENDING", "horizon": "SWING", "profile": "wide_position",
    "direction": "SHORT", "regime": "RANGE", "sl_atr_mult": 3.0, "tp1_rr": 3.0,
    "tp2_rr": 6.0, "consensus_strength": 0.4, "conviction": 0.3, "score": 55,
}


def _feed(eng, features, r, n):
    for _ in range(n):
        eng.record_trade(features, r)


# ── 1. Feature extraction ───────────────────────────────────────────────────


def test_extract_normalises_numeric_to_unit_range():
    fv = TradeFeatureExtractor().extract(
        {"sl_atr_mult": 10.0, "score": 80, "consensus_strength": 0.8}, 1.0,
    )
    assert _close(fv.numeric["sl_atr_mult"], 1.0)
    assert _close(fv.numeric["score"], 0.8)
    assert _close(fv.numeric["consensus_strength"], 0.8)
    for v in fv.numeric.values():
        assert 0.0 <= v <= 1.0


def test_extract_missing_numeric_is_neutral_midpoint():
    fv = TradeFeatureExtractor().extract({"entry_mode": "MARKET"}, 0.5)
    assert _close(fv.numeric["sl_atr_mult"], 0.5)
    assert _close(fv.numeric["tp1_rr"], 0.5)


def test_extract_categorical_uppercased_missing_is_na():
    fv = TradeFeatureExtractor().extract({"entry_mode": "market"}, 1.0)
    assert fv.categorical["entry_mode"] == "MARKET"
    assert fv.categorical["regime"] == "NA"


def test_extract_time_bucket_from_hour():
    e = TradeFeatureExtractor()
    a = e.extract({"hour": 2}, 1.0)    # bucket 0
    b = e.extract({"hour": 10}, 1.0)   # bucket 2 of 6 → 2/5
    assert _close(a.numeric["time_of_day"], 0.0)
    assert _close(b.numeric["time_of_day"], 2.0 / 5.0)


def test_extract_non_finite_r_becomes_zero():
    fv = TradeFeatureExtractor().extract({"entry_mode": "MARKET"}, float("nan"))
    assert fv.r == 0.0


# ── 2. Distance metric ──────────────────────────────────────────────────────


def test_distance_identical_is_zero():
    fv = TradeFeatureExtractor().extract(_SCALP, 1.0)
    assert _close(feature_distance(fv, fv), 0.0)


def test_distance_all_categorical_differ_is_half():
    a = FeatureVector(numeric={"x": 0.5}, categorical={"k": "A"})
    b = FeatureVector(numeric={"x": 0.5}, categorical={"k": "B"})
    # numeric identical (0), categorical fully different (1) → 0.5*0 + 0.5*1
    assert _close(feature_distance(a, b), 0.5)


def test_distance_scalp_vs_swing_is_large():
    e = TradeFeatureExtractor()
    d = feature_distance(e.extract(_SCALP, 1.0), e.extract(_SWING, -1.0))
    assert d > 0.4


# ── 3. Clustering ───────────────────────────────────────────────────────────


def test_clusterer_separates_two_groups():
    e = TradeFeatureExtractor()
    vecs = [e.extract(_SCALP, 1.0) for _ in range(15)] + [
        e.extract(_SWING, -1.0) for _ in range(15)
    ]
    labels = BehaviorClusterer(eps=0.2, min_cluster_size=10, max_clusters=15).cluster(vecs)
    assert len(set(lab for lab in labels if lab >= 0)) == 2
    # First 15 share a label distinct from the last 15.
    assert labels[0] >= 0 and labels[-1] >= 0
    assert labels[0] != labels[-1]


def test_clusterer_below_min_size_is_all_noise():
    e = TradeFeatureExtractor()
    vecs = [e.extract(_SCALP, 1.0) for _ in range(5)]
    labels = BehaviorClusterer(eps=0.2, min_cluster_size=10, max_clusters=15).cluster(vecs)
    assert all(lab == -1 for lab in labels)


def test_clusterer_caps_to_max_clusters():
    e = TradeFeatureExtractor()
    g1 = {**_SCALP}
    g2 = {**_SWING}
    g3 = {**_SCALP, "entry_mode": "LIMIT", "profile": "momentum_chase",
          "direction": "SHORT", "regime": "VOLATILE"}
    vecs = (
        [e.extract(g1, 1.0) for _ in range(12)]
        + [e.extract(g2, 1.0) for _ in range(12)]
        + [e.extract(g3, 1.0) for _ in range(12)]
    )
    labels = BehaviorClusterer(eps=0.2, min_cluster_size=10, max_clusters=2).cluster(vecs)
    assert len(set(lab for lab in labels if lab >= 0)) == 2


# ── 4. Scoring + Bayesian shrinkage ─────────────────────────────────────────


def test_scorer_expectancy_and_win_rate():
    s = BehaviorScorer(min_trades=5, prior_trades=10).score([1.0, 1.0, -1.0, 1.0], 0.0)
    assert s.trades == 4
    assert _close(s.win_rate, 0.75)
    assert _close(s.expectancy, 0.5)


def test_scorer_shrinks_thin_cluster_toward_global():
    # A 3-trade +3R cluster with a strong prior pulls toward the book mean (0).
    s = BehaviorScorer(min_trades=2, prior_trades=100).score([3.0, 3.0, 3.0], 0.0)
    assert s.expectancy == pytest.approx(3.0)
    assert s.shrunk_expectancy < 0.2  # heavily shrunk toward 0


def test_scorer_empty_is_zero():
    s = BehaviorScorer(min_trades=2, prior_trades=10).score([], 0.0)
    assert s.trades == 0 and s.expectancy == 0.0


# ── 5. Recompute gating ─────────────────────────────────────────────────────


def test_maybe_recompute_below_floor_returns_none(db_path):
    eng = _make(db_path, min_trades_to_cluster=100)
    _feed(eng, _SCALP, 1.0, 20)
    assert eng.maybe_recompute(20) is None


def test_maybe_recompute_respects_interval(db_path):
    eng = _make(db_path, min_trades_to_cluster=40, recluster_every_n_trades=50)
    _feed(eng, _SCALP, 1.0, 40)
    _feed(eng, _SWING, -1.0, 40)
    first = eng.maybe_recompute(80)
    assert first is not None
    # Only 10 more trades since → below the 50 interval.
    assert eng.maybe_recompute(90) is None
    # 50 more → due again.
    assert eng.maybe_recompute(130) is not None


def test_maybe_recompute_disabled_returns_none(db_path):
    eng = _make(db_path, enabled=False)
    assert eng.maybe_recompute(10_000) is None


# ── 6. Ingestion guards ─────────────────────────────────────────────────────


def test_record_trade_disabled_is_noop(db_path):
    eng = _make(db_path, enabled=False)
    eng.record_trade(_SCALP, 1.0)
    assert eng.total_trades() == 0


def test_record_trade_empty_features_is_noop(db_path):
    eng = _make(db_path)
    eng.record_trade({}, 1.0)
    assert eng.total_trades() == 0


def test_record_trade_non_finite_r_skipped(db_path):
    eng = _make(db_path)
    eng.record_trade(_SCALP, float("inf"))
    assert eng.total_trades() == 0


# ── 7. Lifecycle ────────────────────────────────────────────────────────────


def test_winning_cluster_promotes_to_active(db_path):
    eng = _make(db_path)
    _feed(eng, _SCALP, 1.5, 40)
    _feed(eng, _SWING, -1.0, 40)
    payload = eng.recompute()
    states = {b["centroid"]["categorical"].get("profile"): b["state"]
              for b in payload["behaviors"]}
    assert states.get("TIGHT_SCALP") == ACTIVE
    # The losing cluster is never promoted (stays SHADOW).
    assert states.get("WIDE_POSITION") == SHADOW


def test_active_behaviour_retires_when_it_drops_to_bottom(db_path):
    eng = _make(db_path, recluster_every_n_trades=1, cooldown_trades=0)
    # Pass 1: scalp winning → ACTIVE, swing mediocre.
    _feed(eng, _SCALP, 1.5, 40)
    _feed(eng, _SWING, 0.1, 40)
    p1 = eng.recompute()
    scalp1 = [b for b in p1["behaviors"] if b["centroid"]["categorical"].get("profile") == "TIGHT_SCALP"][0]
    assert scalp1["state"] == ACTIVE
    # Pass 2: scalp now strongly losing, swing strongly winning → scalp bottom
    # percentile → ACTIVE → RETIRED.
    _feed(eng, _SCALP, -2.0, 80)
    _feed(eng, _SWING, 2.0, 80)
    p2 = eng.recompute()
    scalp2 = [b for b in p2["behaviors"] if b["centroid"]["categorical"].get("profile") == "TIGHT_SCALP"][0]
    assert scalp2["state"] == RETIRED


def test_stale_active_behaviour_retires(db_path):
    eng = _make(db_path)
    eng._pass_counter = 10
    rec = BehaviorRecord(
        behavior_id="bhv_x", state=ACTIVE, trades=50, shrunk_expectancy=0.5,
        last_seen_pass=1,  # not seen for 9 passes
    )
    behaviors = {"bhv_x": rec}
    eng._apply_lifecycle(behaviors, total_trades=10_000)
    assert behaviors["bhv_x"].state == RETIRED


def test_cooldown_blocks_transition(db_path):
    eng = _make(db_path, cooldown_trades=100_000)
    eng._pass_counter = 1
    rec = BehaviorRecord(
        behavior_id="bhv_y", state=SHADOW, trades=50, shrunk_expectancy=5.0,
        percentile=1.0, last_seen_pass=1, last_action_trades=0,
    )
    behaviors = {"bhv_y": rec}
    eng._apply_lifecycle(behaviors, total_trades=10)  # within cooldown
    assert behaviors["bhv_y"].state == SHADOW


# ── 8. Behaviour continuity (centroid matching) ─────────────────────────────


def test_behaviour_id_persists_across_passes(db_path):
    eng = _make(db_path, recluster_every_n_trades=1)
    _feed(eng, _SCALP, 1.5, 40)
    _feed(eng, _SWING, -1.0, 40)
    p1 = eng.recompute()
    ids1 = {b["behavior_id"] for b in p1["behaviors"]}
    _feed(eng, _SCALP, 1.5, 5)
    p2 = eng.recompute()
    ids2 = {b["behavior_id"] for b in p2["behaviors"]}
    # The same behaviours should be re-matched, not re-created wholesale.
    assert ids1 & ids2


# ── 9. Persistence ──────────────────────────────────────────────────────────


def test_persistence_round_trip(db_path):
    eng = _make(db_path)
    _feed(eng, _SCALP, 1.5, 40)
    _feed(eng, _SWING, -1.0, 40)
    eng.recompute()
    eng.close()
    # Re-open against the same DB — behaviours + recorded trades survive.
    eng2 = _make(db_path)
    assert eng2.total_trades() == 80
    state = eng2.get_state()
    assert state["behavior_count"] >= 1
    cached = eng2.get_cached()
    assert cached["behavior_count"] >= 1


# ── 10. Empty / dormant state ───────────────────────────────────────────────


def test_empty_engine_is_dormant(db_path):
    eng = _make(db_path)
    state = eng.get_state()
    assert state["behavior_count"] == 0
    assert state["cluster_count"] == 0
    assert state["total_trades"] == 0
    # recompute below the floor returns the empty shape, never raises.
    _feed(eng, _SCALP, 1.0, 5)
    assert eng.recompute()["behavior_count"] == 0


def test_get_state_and_dashboard_shape(db_path):
    eng = _make(db_path)
    _feed(eng, _SCALP, 1.5, 40)
    _feed(eng, _SWING, -1.0, 40)
    eng.recompute()
    state = eng.get_dashboard_data()
    for key in ("enabled", "lookback", "interval", "total_trades",
                "behavior_count", "cluster_count", "stability", "counts",
                "behaviors"):
        assert key in state
    assert isinstance(state["behaviors"], list)
    assert isinstance(state["counts"], dict)


# ── 11. Stability ───────────────────────────────────────────────────────────


def test_first_pass_stability_is_one(db_path):
    eng = _make(db_path)
    _feed(eng, _SCALP, 1.5, 40)
    _feed(eng, _SWING, -1.0, 40)
    p = eng.recompute()
    assert _close(p["stability"], 1.0)


# ── 12. Tunable adapter ─────────────────────────────────────────────────────


def test_tunable_protocol_compliance(db_path):
    eng = _make(db_path)
    t = BehaviorDiscoveryTunable(eng, min_trades=40)
    assert isinstance(t, Tunable)
    assert t.tunable_name == "behavior_discovery"
    assert t.frequency == TuneFrequency.ON_TRADE_BATCH
    assert t.min_trades_required == 40
    ok, _ = t.validate_params(t.get_current_params())
    assert ok
    assert t.rollback() is True


def test_tunable_tune_runs_recompute(db_path):
    eng = _make(db_path, min_trades_to_cluster=40)
    _feed(eng, _SCALP, 1.5, 40)
    _feed(eng, _SWING, -1.0, 40)
    t = BehaviorDiscoveryTunable(eng, min_trades=40)
    res = t.tune(TuneContext(total_trades=80, force=True))
    assert res.success
    assert not res.skipped
    assert "cluster" in res.reason


def test_tunable_tune_disabled_skips(db_path):
    eng = _make(db_path, enabled=False)
    t = BehaviorDiscoveryTunable(eng, min_trades=40)
    res = t.tune(TuneContext(total_trades=10_000, force=True))
    assert res.success and res.skipped


# ── 13. Config ──────────────────────────────────────────────────────────────


def test_config_defaults_active():
    cfg = BehaviorDiscoveryConfig()
    assert cfg.behavior_discovery_enabled is True
    assert cfg.min_trades_to_cluster == 100
    assert cfg.min_cluster_size == 20


def test_config_rejects_bad_thresholds():
    with pytest.raises(ValueError):
        BehaviorDiscoveryConfig(promote_threshold=0.2, retire_threshold=0.5)
    with pytest.raises(ValueError):
        BehaviorDiscoveryConfig(cluster_eps=2.0)
    with pytest.raises(ValueError):
        BehaviorDiscoveryConfig(min_cluster_size=1)

"""
Tests for the Execution Style Profiles engine (adaptive/execution_profiles.py,
L5.5b) and its TunerAgent adapter
(adaptive.tunable_adapters.ExecutionProfileTunable), plus the additive entry
engine SL/TP/min-score overrides and the capital-allocator fingerprint
enrichment they feed.

Covers built-in seeding, profile lookup/creation/retirement, context-aware
selection (horizon × regime × consensus), the default-profile fallback, SQLite
persistence round-trips, the disabled no-op, the active-profiles cap, the
per-trade management override mapping, the max-hold helper, Tunable protocol
compliance, the entry-engine parameter overrides, fingerprint enrichment, and
ZERO REGRESSION (profiles disabled / None ⇒ identical to the pre-profile path).

Deterministic: temp SQLite DBs so nothing touches the real data/ dir.
"""

import os
import tempfile

import pandas as pd
import pytest

from config import AppConfig, ExecutionProfileConfig
from adaptive.execution_profiles import (
    ExecutionProfile,
    ExecutionProfileManager,
    TRAILING_METHODS,
)
from adaptive.capital_allocator import compute_fingerprint
from adaptive.tunable import Tunable, TuneContext, TuneFrequency
from adaptive.tunable_adapters import ExecutionProfileTunable
from trigger.entry_engine import EntryEngine


# Several sibling test modules stub ``pandas``/``numpy`` into ``sys.modules`` at
# import time and never restore them, which turns ``pd.DataFrame`` into a
# MagicMock for any test collected afterwards. The pandas-dependent entry-engine
# tests below run fully in isolation; in a contaminated full-suite run they
# skip cleanly rather than false-fail on the stub.
try:
    _PANDAS_REAL = hasattr(pd.DataFrame([{"a": 1}]), "iloc")
except Exception:  # noqa: BLE001
    _PANDAS_REAL = False

_needs_pandas = pytest.mark.skipif(
    not _PANDAS_REAL, reason="pandas stubbed by a sibling test module (isolation artifact)"
)


BUILTINS = {
    "tight_scalp",
    "standard_swing",
    "wide_position",
    "momentum_chase",
    "counter_trend_fade",
}


# ── Fixtures / helpers ──────────────────────────────────────────────────────


def _close(a, b, eps=1e-3):
    """Tolerant float compare.

    Used instead of ``pytest.approx`` because a sibling test module installs a
    stubbed ``numpy`` into ``sys.modules`` at import time, which breaks
    ``pytest.approx`` (it probes ``np.bool_``) for any test collected after it.
    """
    return abs(float(a) - float(b)) <= eps


def _mgr(tmp_path=None, **kw) -> ExecutionProfileManager:
    d = tmp_path or tempfile.mkdtemp()
    db = os.path.join(str(d), f"ep_{os.getpid()}_{id(d) % 100000}.db")
    return ExecutionProfileManager(db_path=db, **kw)


# ── Config ──────────────────────────────────────────────────────────────────


def test_config_defaults_active():
    cfg = ExecutionProfileConfig()
    assert cfg.enabled is True
    assert cfg.default_profile == "standard_swing"
    assert cfg.allow_profile_creation is True
    assert AppConfig().execution_profiles.enabled is True


def test_config_validation_rejects_bad_bounds():
    with pytest.raises(ValueError):
        ExecutionProfileConfig(max_active_profiles=0)
    with pytest.raises(ValueError):
        ExecutionProfileConfig(weak_consensus_threshold=0.9, strong_consensus_threshold=0.5)
    with pytest.raises(ValueError):
        ExecutionProfileConfig(default_profile="")


# ── Built-in seeding + lookup ───────────────────────────────────────────────


def test_builtins_seeded_on_first_run():
    m = _mgr(enabled=True)
    try:
        names = {p.name for p in m.active_profiles()}
        assert names == BUILTINS
    finally:
        m.close()


def test_get_profile_returns_known_params():
    m = _mgr(enabled=True)
    try:
        p = m.get_profile("tight_scalp")
        assert p is not None
        assert p.sl_atr_multiplier == 1.0
        assert p.tp_rr_ratio == 1.5
        assert p.tp2_rr_ratio == 2.0
        assert p.trailing_method == "breakeven_only"
        assert m.get_profile("does_not_exist") is None
    finally:
        m.close()


# ── Selection ───────────────────────────────────────────────────────────────


def test_select_by_horizon():
    m = _mgr(enabled=True)
    try:
        assert m.select_profile(horizon="SCALP", regime="RANGING", consensus_strength=0.6).name == "tight_scalp"
        assert m.select_profile(horizon="SWING", regime="", consensus_strength=0.5).name == "standard_swing"
        # No horizon → configured default.
        assert m.select_profile(horizon="", regime="", consensus_strength=0.5).name == "standard_swing"
    finally:
        m.close()


def test_select_refined_by_regime_and_consensus():
    m = _mgr(enabled=True)
    try:
        # Strong trend + strong consensus widens a swing idea.
        assert m.select_profile(horizon="SWING", regime="TRENDING", consensus_strength=0.9).name == "wide_position"
        # Volatile/trending scalp chases momentum.
        assert m.select_profile(horizon="SCALP", regime="VOLATILE", consensus_strength=0.5).name == "momentum_chase"
        # Ranging + weak consensus → mean-reversion fade (overrides horizon base).
        assert m.select_profile(horizon="SWING", regime="RANGING", consensus_strength=0.2).name == "counter_trend_fade"
    finally:
        m.close()


def test_select_disabled_returns_none_noop():
    m = _mgr(enabled=False)
    try:
        assert m.select_profile(horizon="SCALP", regime="TRENDING", consensus_strength=0.9) is None
    finally:
        m.close()


def test_select_falls_back_to_default_when_target_retired():
    m = _mgr(enabled=True)
    try:
        # Retire the profile a SCALP/range context would pick → must fall back.
        assert m.retire_profile("tight_scalp") is True
        chosen = m.select_profile(horizon="SCALP", regime="RANGING", consensus_strength=0.6)
        assert chosen is not None
        assert chosen.name != "tight_scalp"
        assert chosen.active is True
    finally:
        m.close()


# ── Creation / retirement / cap ─────────────────────────────────────────────


def test_create_profile_and_cap():
    m = _mgr(enabled=True, allow_profile_creation=True, max_active_profiles=6)
    try:
        created = m.create_profile(ExecutionProfile(name="experimental", sl_atr_multiplier=2.5))
        assert created is not None
        assert m.get_profile("experimental") is not None
        # 5 builtins + 1 created = 6 (cap). Next active create is refused.
        refused = m.create_profile(ExecutionProfile(name="too_many"))
        assert refused is None
    finally:
        m.close()


def test_create_refused_when_disabled():
    m = _mgr(enabled=True, allow_profile_creation=False)
    try:
        assert m.create_profile(ExecutionProfile(name="nope")) is None
    finally:
        m.close()


def test_retire_profile_excludes_from_active():
    m = _mgr(enabled=True)
    try:
        assert m.retire_profile("wide_position") is True
        assert "wide_position" not in {p.name for p in m.active_profiles()}
        assert m.retire_profile("ghost") is False
    finally:
        m.close()


# ── Persistence ─────────────────────────────────────────────────────────────


def test_persistence_round_trip():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "persist.db")
    m1 = ExecutionProfileManager(db_path=db, enabled=True)
    try:
        m1.create_profile(ExecutionProfile(name="persisted", sl_atr_multiplier=2.7, tp_rr_ratio=2.2))
        m1.retire_profile("counter_trend_fade")
    finally:
        m1.close()
    m2 = ExecutionProfileManager(db_path=db, enabled=True)
    try:
        p = m2.get_profile("persisted")
        assert p is not None and p.sl_atr_multiplier == 2.7
        assert "counter_trend_fade" not in {x.name for x in m2.active_profiles()}
    finally:
        m2.close()


# ── Management override mapping ─────────────────────────────────────────────


def test_plan_trail_strategy_mapping():
    scalp = ExecutionProfile(name="s", trailing_method="breakeven_only")
    swing = ExecutionProfile(name="w", trailing_method="structure")
    assert scalp.plan_trail_strategy() == "none"   # BE-only disables trailing
    assert swing.plan_trail_strategy() == "structure"


def test_plan_partial_ratio_mapping():
    on = ExecutionProfile(name="a", partial_exit_enabled=True, partial_exit_pct=0.5)
    off = ExecutionProfile(name="b", partial_exit_enabled=False)
    assert _close(on.plan_partial_ratio(), 0.5)
    assert off.plan_partial_ratio() is None  # defer to global default


def test_max_hold_exceeded():
    p = ExecutionProfile(name="c", max_hold_bars=50)
    unlimited = ExecutionProfile(name="d", max_hold_bars=0)
    assert p.max_hold_exceeded(49) is False
    assert p.max_hold_exceeded(50) is True
    assert unlimited.max_hold_exceeded(10_000) is False


def test_trailing_method_clamp_to_known():
    p = ExecutionProfile(name="x", trailing_method="bogus").clamp()
    assert p.trailing_method in TRAILING_METHODS
    assert p.trailing_method == "structure"


# ── Outcome ingestion + state ───────────────────────────────────────────────


def test_record_outcome_expectancy_and_state():
    m = _mgr(enabled=True, min_trades_for_scoring=3)
    try:
        for r in (1.0, 2.0, -1.0):  # mean = 0.667
            m.record_outcome("tight_scalp", r)
        st = m.get_state()
        row = next(p for p in st["profiles"] if p["name"] == "tight_scalp")
        assert row["trades"] == 3
        assert _close(row["expectancy"], 0.6667, eps=1e-3)
        assert row["scored"] is True
        assert st["total_trades"] == 3
    finally:
        m.close()


# ── Capital-allocator fingerprint enrichment ────────────────────────────────


def test_fingerprint_enriched_with_profile():
    base = compute_fingerprint("MARKET", "SCALP")
    enriched = compute_fingerprint("MARKET", "SCALP", extra="tight_scalp")
    assert base == "MARKET|SCALP"
    assert enriched == "MARKET|SCALP|TIGHT_SCALP"
    assert base != enriched


# ── Tunable protocol ────────────────────────────────────────────────────────


def test_tunable_protocol_compliance():
    m = _mgr(enabled=True)
    try:
        t = ExecutionProfileTunable(m)
        assert isinstance(t, Tunable)
        assert t.tunable_name == "execution_profiles"
        assert t.frequency == TuneFrequency.ON_DEMAND
        # snapshot → mutate → rollback
        before = t.get_current_params()
        assert before  # non-empty (active profile knobs)
        ok, _ = t.validate_params({"tight_scalp.sl_atr_multiplier": 1.7})
        assert ok
        bad, _ = t.validate_params({"tight_scalp.sl_atr_multiplier": 999.0})
        assert not bad
        t._begin()
        m.apply_params({"tight_scalp.sl_atr_multiplier": 1.7})
        assert _close(m.get_profile("tight_scalp").sl_atr_multiplier, 1.7)
        assert t.rollback() is True
        assert _close(m.get_profile("tight_scalp").sl_atr_multiplier, 1.0)
        # tune() is a no-op skip (no autonomous recompute).
        res = t.tune(TuneContext(force=True))
        assert res.skipped is True and res.success is True
    finally:
        m.close()


def test_apply_params_clamps_out_of_bounds():
    m = _mgr(enabled=True)
    try:
        m.apply_params({"tight_scalp.sl_atr_multiplier": 99.0})  # > 10.0 cap
        assert m.get_profile("tight_scalp").sl_atr_multiplier <= 10.0
    finally:
        m.close()


# ── Entry-engine override wiring + ZERO REGRESSION ──────────────────────────


def _flat_h1(base: float = 1.2700, n: int = 60) -> pd.DataFrame:
    rows = [
        {
            "time": pd.Timestamp("2025-01-01") + pd.Timedelta(hours=i),
            "open": base, "high": base + 0.0002, "low": base - 0.0002, "close": base,
        }
        for i in range(n)
    ]
    return pd.DataFrame(rows)


@_needs_pandas
def test_calculate_targets_default_matches_pre_profile():
    """No override args ⇒ TP1 1.5R / TP2 3.0R — the pre-profile constants."""
    eng = EntryEngine(config=AppConfig())
    entry, sl = 1.2710, 1.2700  # risk = 0.0010
    risk = abs(entry - sl)
    tp1, tp2 = eng.calculate_targets("EURUSD", "LONG", entry, sl, _flat_h1(), 0.0001)
    assert _close((tp1 - entry) / risk, 1.5, eps=1e-4)
    assert _close((tp2 - entry) / risk, 3.0, eps=1e-4)


@_needs_pandas
def test_calculate_targets_profile_rr_override():
    eng = EntryEngine(config=AppConfig())
    entry, sl = 1.2710, 1.2700
    risk = abs(entry - sl)
    tp1, tp2 = eng.calculate_targets(
        "EURUSD", "LONG", entry, sl, _flat_h1(), 0.0001, tp1_rr=2.0, tp2_rr=6.0,
    )
    assert _close((tp1 - entry) / risk, 2.0, eps=1e-4)
    assert _close((tp2 - entry) / risk, 6.0, eps=1e-4)


def _atr_m5(base: float = 1.2701, n: int = 40, half_range: float = 0.0015) -> pd.DataFrame:
    rows = [
        {
            "time": pd.Timestamp("2025-01-01") + pd.Timedelta(minutes=5 * i),
            "open": base, "high": base + half_range, "low": base - half_range, "close": base,
        }
        for i in range(n)
    ]
    return pd.DataFrame(rows)


@_needs_pandas
def test_stop_loss_atr_mult_override_changes_distance():
    cfg = AppConfig()
    cfg.risk.volatility_stop_mode = "on"
    cfg.risk.atr_stop_period = 14
    cfg.risk.atr_stop_mult = 1.5
    cfg.risk.atr_stop_ratio_min = 0.1
    cfg.risk.atr_stop_ratio_max = 10.0
    cfg.risk.atr_stop_max_risk_mult = 20.0
    eng = EntryEngine(config=cfg)
    zone = {"bottom": 1.27000, "top": 1.27020, "midpoint": 1.27010}
    entry = 1.27010
    # Small ATR so both multiples stay inside the [min, max] ratio band (and
    # under the max-risk cap) — otherwise both clamp to the same ceiling.
    base = eng.calculate_stop_loss("LONG", zone, 0.0001, entry_price=entry, pair="EURUSD", m5_df=_atr_m5(half_range=0.00015))
    wide = eng.calculate_stop_loss(
        "LONG", zone, 0.0001, entry_price=entry, pair="EURUSD", m5_df=_atr_m5(half_range=0.00015),
        atr_mult_override=3.0,
    )
    base_dist = abs(entry - base)
    wide_dist = abs(entry - wide)
    assert wide_dist > base_dist  # a larger ATR multiple widens the stop


@_needs_pandas
def test_stop_loss_no_override_matches_instance_default():
    cfg = AppConfig()
    cfg.risk.volatility_stop_mode = "on"
    cfg.risk.atr_stop_period = 14
    eng = EntryEngine(config=cfg)
    zone = {"bottom": 1.27000, "top": 1.27020, "midpoint": 1.27010}
    entry = 1.27010
    a = eng.calculate_stop_loss("LONG", zone, 0.0001, entry_price=entry, pair="EURUSD", m5_df=_atr_m5())
    b = eng.calculate_stop_loss(
        "LONG", zone, 0.0001, entry_price=entry, pair="EURUSD", m5_df=_atr_m5(),
        atr_mult_override=None,
    )
    assert a == b  # None override ⇒ identical to the instance default

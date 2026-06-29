"""Tests for the ThesisEngine (Gap 1a — persistent competing theses)."""

from __future__ import annotations

import threading

from brain.directional_consensus import Vote
from brain.thesis_engine import FLAT, LONG, SHORT, ThesisEngine


def _engine(**kw) -> ThesisEngine:
    return ThesisEngine(**kw)


def _vote(module: str, direction: str, confidence: float, weight: float = 1.0) -> Vote:
    return Vote(module=module, direction=direction, confidence=confidence, weight=weight)


# ── 1. Competing theses are created simultaneously ───────────────────────


def test_competing_theses_created():
    eng = _engine()
    votes = [_vote("structure", LONG, 0.8), _vote("momentum", SHORT, 0.4)]
    tset = eng.update("EURUSD", votes, 0.7, 0.3, entry_ev_long=0.5, entry_ev_short=-0.2)
    assert tset is not None
    # All three theses exist at once — the losing side is not discarded.
    assert tset.long_thesis.direction == LONG
    assert tset.short_thesis.direction == SHORT
    assert tset.flat_thesis.direction == FLAT
    assert "structure" in tset.long_thesis.supporting_modules
    assert "momentum" in tset.short_thesis.supporting_modules


# ── 2. Flat thesis is always available ───────────────────────────────────


def test_flat_always_available():
    eng = _engine()
    # No votes at all.
    tset = eng.update("EURUSD", [], 0.0, 0.0, entry_ev_long=-1.0, entry_ev_short=-1.0)
    assert tset is not None
    assert tset.flat_thesis.direction == FLAT
    # With both directional EVs negative, Flat (EV=0) dominates.
    assert tset.dominant == FLAT


# ── 3. Dominant is the highest-EV thesis ─────────────────────────────────


def test_dominant_is_highest_ev():
    eng = _engine()
    votes = [_vote("structure", LONG, 0.9), _vote("fvg", LONG, 0.8)]
    tset = eng.update("EURUSD", votes, 0.8, 0.2, entry_ev_long=0.9, entry_ev_short=-0.4)
    assert tset.dominant == LONG
    # Advantage = best EV (0.9 long) minus second-best (0.0 flat).
    assert abs(tset.dominant_ev_advantage - 0.9) < 1e-9


def test_short_dominates_when_short_ev_highest():
    eng = _engine()
    votes = [_vote("structure", SHORT, 0.9)]
    tset = eng.update("EURUSD", votes, 0.2, 0.8, entry_ev_long=-0.5, entry_ev_short=1.1)
    assert tset.dominant == SHORT


# ── 4. Flat dominates when directional EV is low ─────────────────────────


def test_flat_dominates_when_ev_low():
    eng = _engine(flat_ev=0.0)
    votes = [_vote("structure", LONG, 0.6)]
    # Long EV barely positive but below Flat — actually negative here.
    tset = eng.update("EURUSD", votes, 0.5, 0.5, entry_ev_long=-0.1, entry_ev_short=-0.3)
    assert tset.dominant == FLAT


# ── 5. should_act respects the flat baseline ─────────────────────────────


def test_should_act_requires_ev_advantage_over_flat():
    eng = _engine(min_ev_threshold=0.3)
    votes = [_vote("structure", LONG, 0.9)]
    # Long EV = 0.2, only 0.2 over Flat (0.0) — below the 0.3 threshold.
    eng.update("EURUSD", votes, 0.7, 0.3, entry_ev_long=0.2, entry_ev_short=-0.5)
    act, direction, advantage = eng.should_act("EURUSD")
    assert act is False
    assert direction == FLAT


def test_should_act_fires_when_advantage_clears_threshold():
    eng = _engine(min_ev_threshold=0.3)
    votes = [_vote("structure", LONG, 0.9)]
    eng.update("EURUSD", votes, 0.8, 0.2, entry_ev_long=0.6, entry_ev_short=-0.5)
    act, direction, advantage = eng.should_act("EURUSD")
    assert act is True
    assert direction == LONG
    assert advantage >= 0.3


def test_should_act_unknown_symbol_is_flat():
    eng = _engine()
    act, direction, advantage = eng.should_act("NOPE")
    assert act is False
    assert direction == FLAT
    assert advantage == 0.0


# ── 6. Decay reduces stale theses; Flat never decays ─────────────────────


def test_decay_reduces_directional_confidence():
    eng = _engine(decay_rate=0.5)
    votes = [_vote("structure", LONG, 0.8)]
    tset = eng.update("EURUSD", votes, 0.8, 0.2, entry_ev_long=0.6, entry_ev_short=-0.4)
    before = tset.long_thesis.confidence
    assert before > 0.0
    eng.decay_all()
    after = eng.get("EURUSD").long_thesis.confidence
    assert after < before
    assert abs(after - before * 0.5) < 1e-9
    assert eng.get("EURUSD").long_thesis.age_bars == 1
    assert eng.get("EURUSD").long_thesis.decay_factor == 0.5


def test_flat_never_decays():
    eng = _engine(decay_rate=0.5)
    votes = [_vote("structure", LONG, 0.8)]
    eng.update("EURUSD", votes, 0.8, 0.2, entry_ev_long=0.6, entry_ev_short=-0.4)
    flat_before = eng.get("EURUSD").flat_thesis.confidence
    flat_decay_before = eng.get("EURUSD").flat_thesis.decay_factor
    eng.decay_all()
    flat_after = eng.get("EURUSD").flat_thesis.confidence
    assert flat_after == flat_before
    assert eng.get("EURUSD").flat_thesis.decay_factor == flat_decay_before == 1.0
    assert eng.get("EURUSD").flat_thesis.age_bars == 0


# ── 7. Uncertainty rises with opposition ─────────────────────────────────


def test_uncertainty_from_opposition():
    eng = _engine()
    # Equal long and short magnitude → maximal uncertainty for the long thesis.
    votes = [_vote("structure", LONG, 0.8), _vote("momentum", SHORT, 0.8)]
    tset = eng.update("EURUSD", votes, 0.5, 0.5, entry_ev_long=0.1, entry_ev_short=0.1)
    assert abs(tset.long_thesis.uncertainty - 0.5) < 1e-9
    # No opposition → low uncertainty.
    votes2 = [_vote("structure", LONG, 0.8), _vote("fvg", LONG, 0.8)]
    tset2 = eng.update("GBPUSD", votes2, 0.8, 0.2, entry_ev_long=0.5, entry_ev_short=-0.4)
    assert tset2.long_thesis.uncertainty == 0.0


# ── 8. Update refreshes decay (fresh thesis after re-update) ─────────────


def test_update_refreshes_decay():
    eng = _engine(decay_rate=0.5)
    votes = [_vote("structure", LONG, 0.8)]
    eng.update("EURUSD", votes, 0.8, 0.2, entry_ev_long=0.6, entry_ev_short=-0.4)
    eng.decay_all()
    assert eng.get("EURUSD").long_thesis.decay_factor == 0.5
    # A fresh update resets decay_factor to 1.0 and age to 0.
    eng.update("EURUSD", votes, 0.8, 0.2, entry_ev_long=0.6, entry_ev_short=-0.4)
    assert eng.get("EURUSD").long_thesis.decay_factor == 1.0
    assert eng.get("EURUSD").long_thesis.age_bars == 0


# ── 9. ev_advantage is the best-minus-second-best gap ────────────────────


def test_ev_advantage_computed():
    eng = _engine()
    votes = [_vote("structure", LONG, 0.9)]
    tset = eng.update("EURUSD", votes, 0.8, 0.2, entry_ev_long=1.2, entry_ev_short=0.4)
    # best=1.2 (long), second-best=0.4 (short) → advantage 0.8.
    assert abs(tset.dominant_ev_advantage - 0.8) < 1e-9


# ── 10. Thread safety ────────────────────────────────────────────────────


def test_thread_safety():
    eng = _engine()
    votes = [_vote("structure", LONG, 0.8)]

    def worker(sym: str) -> None:
        for _ in range(200):
            eng.update(sym, votes, 0.7, 0.3, entry_ev_long=0.5, entry_ev_short=-0.2)
            eng.get(sym)
            eng.should_act(sym)
            eng.decay_all()
            eng.get_status()

    threads = [threading.Thread(target=worker, args=(f"S{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    status = eng.get_status()
    assert status["tracked_symbols"] == 6


# ── 11. Fail-safe on malformed input ─────────────────────────────────────


def test_fail_safe_on_bad_votes():
    eng = _engine()

    class _BadVote:
        @property
        def direction(self):
            raise RuntimeError("boom")

    # A vote that raises on attribute access must not crash update().
    tset = eng.update("EURUSD", [_BadVote()], 0.5, 0.5, entry_ev_long=0.1, entry_ev_short=0.1)
    # Either a safe ThesisSet or None — never an exception propagated.
    assert tset is None or tset.symbol == "EURUSD"


def test_fail_safe_on_nan_probabilities():
    eng = _engine()
    votes = [_vote("structure", LONG, 0.8)]
    tset = eng.update(
        "EURUSD", votes, float("nan"), float("inf"),
        entry_ev_long=float("nan"), entry_ev_short=0.0,
    )
    assert tset is not None
    # NaN/inf coerced to safe finite values.
    assert tset.long_thesis.probability == 0.0
    assert tset.long_thesis.ev == 0.0


# ── 12. get_status summary shape ─────────────────────────────────────────


def test_get_status_shape():
    eng = _engine(min_ev_threshold=0.3)
    votes = [_vote("structure", LONG, 0.9)]
    eng.update("EURUSD", votes, 0.8, 0.2, entry_ev_long=0.6, entry_ev_short=-0.5)
    status = eng.get_status()
    assert status["enabled"] is True
    assert status["tracked_symbols"] == 1
    assert status["actionable"] == 1  # long EV 0.6 clears 0.3 over flat
    assert "EURUSD" in status["theses"]
    assert status["theses"]["EURUSD"]["dominant"] == LONG

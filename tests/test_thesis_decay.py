"""Session 27 — time-based thesis decay + continuous challenge (Gap 2).

The APEX vision: "no opinion is permanent" and "every thesis is continuously
challenged by new evidence". A thesis must keep *earning* its standing — the
absence of confirming evidence is itself disconfirming. This session adds a
TIME-based decay of a thesis's *effective EV*, computed **on read** (never
stored, no timer)::

    effective_ev = base_ev * exp(-ln2 / half_life * seconds_since_refresh)

Coverage:

* The decay formula (0.5 at one half-life, 0.25 at two, ...).
* Decay is applied on READ and never mutates the stored base EV.
* A fresh ``update`` resets the decay clock.
* ``should_act`` / ``get_best_thesis`` use the decayed effective EV.
* A decay-induced dominance flip is detected (fresher opposing thesis overtakes
  a stale leader) and logged edge-triggered.
* Flat also decays (no-op at the default ``flat_ev`` = 0, real when non-zero).
* The master switch disables decay entirely.
* The decay floor marks an expired (dead) thesis.
* Edge-triggered challenge flags fire once and reset on the next update.
* Config validation for the new decay parameters.
"""

from __future__ import annotations

import time

import pytest

from brain.directional_consensus import Vote
from brain.thesis_engine import FLAT, LONG, SHORT, Thesis, ThesisEngine


def _vote(module: str, direction: str, confidence: float, weight: float = 1.0) -> Vote:
    return Vote(module=module, direction=direction, confidence=confidence, weight=weight)


# ── 1. Decay formula ─────────────────────────────────────────────────────


def test_decay_factor_matches_half_life_curve():
    eng = ThesisEngine(decay_half_life=100.0)
    t = Thesis(symbol="X", direction=LONG, ev=1.0, last_refreshed_at=1000.0)
    assert abs(eng._time_decay_factor(t, 1000.0) - 1.0) < 1e-9      # fresh
    assert abs(eng._time_decay_factor(t, 1100.0) - 0.5) < 1e-6      # 1 half-life
    assert abs(eng._time_decay_factor(t, 1200.0) - 0.25) < 1e-6     # 2 half-lives
    assert abs(eng._time_decay_factor(t, 1300.0) - 0.125) < 1e-6    # 3 half-lives


def test_no_refresh_time_means_no_decay():
    # A thesis with no decay clock (last_refreshed_at == 0) is never decayed —
    # the guard avoids treating "unknown age" as "infinitely stale".
    eng = ThesisEngine(decay_half_life=100.0)
    t = Thesis(symbol="X", direction=LONG, ev=1.0, last_refreshed_at=0.0)
    assert eng._time_decay_factor(t, 1e9) == 1.0


def test_future_read_never_amplifies():
    # A read timestamped BEFORE the refresh (clock skew) must not amplify EV.
    eng = ThesisEngine(decay_half_life=100.0)
    t = Thesis(symbol="X", direction=LONG, ev=1.0, last_refreshed_at=1000.0)
    assert eng._time_decay_factor(t, 999.0) == 1.0


# ── 2. Decay is computed on read, base is immutable ──────────────────────


def test_decay_on_read_does_not_mutate_base():
    eng = ThesisEngine(min_ev_threshold=0.1, decay_half_life=100.0)
    eng.update("EURUSD", [_vote("structure", LONG, 0.9)], 0.8, 0.2,
               entry_ev_long=0.6, entry_ev_short=-0.4)
    thesis = eng.get("EURUSD").long_thesis
    t0 = thesis.last_refreshed_at
    base = thesis.ev

    eff = eng._effective_ev(thesis, t0 + 200.0)     # 2 half-lives → 25%
    assert abs(eff - base * 0.25) < 1e-6
    assert eff < base
    # Stored base EV + refresh clock are untouched by the read.
    assert eng.get("EURUSD").long_thesis.ev == base
    assert eng.get("EURUSD").long_thesis.last_refreshed_at == t0


# ── 3. A fresh update resets the decay clock ─────────────────────────────


def test_update_resets_decay_clock():
    eng = ThesisEngine(min_ev_threshold=0.1, decay_half_life=100.0)
    eng.update("EURUSD", [_vote("structure", LONG, 0.9)], 0.8, 0.2,
               entry_ev_long=0.6, entry_ev_short=-0.4)
    first = eng.get("EURUSD").long_thesis.last_refreshed_at
    time.sleep(0.01)
    eng.update("EURUSD", [_vote("structure", LONG, 0.9)], 0.8, 0.2,
               entry_ev_long=0.6, entry_ev_short=-0.4)
    second = eng.get("EURUSD").long_thesis.last_refreshed_at
    assert second >= first
    # Reading right at the refresh instant → no decay.
    fresh = eng.get("EURUSD").long_thesis
    assert abs(eng._time_decay_factor(fresh, second) - 1.0) < 1e-9


# ── 4. should_act / get_best_thesis use the decayed EV ───────────────────


def test_should_act_uses_decayed_ev():
    eng = ThesisEngine(min_ev_threshold=0.3, decay_half_life=100.0)
    eng.update("EURUSD", [_vote("structure", LONG, 0.9)], 0.9, 0.1,
               entry_ev_long=0.6, entry_ev_short=-0.4)
    t0 = eng.get("EURUSD").long_thesis.last_refreshed_at

    # Fresh: 0.6 over flat clears the 0.3 margin.
    act, direction, adv = eng.should_act("EURUSD", now=t0)
    assert act is True and direction == LONG
    assert abs(adv - 0.6) < 1e-6

    # After 2 half-lives the effective edge (0.15) is below the margin — the
    # stale thesis is no longer actionable even though base EV is unchanged.
    act2, dir2, adv2 = eng.should_act("EURUSD", now=t0 + 200.0)
    assert act2 is False and dir2 == FLAT
    assert adv2 < 0.3
    assert eng.get("EURUSD").long_thesis.ev == 0.6   # base untouched


def test_get_best_thesis_reflects_decay():
    eng = ThesisEngine(min_ev_threshold=0.1, decay_half_life=100.0)
    eng.update("EURUSD", [_vote("structure", LONG, 0.9)], 0.9, 0.1,
               entry_ev_long=0.6, entry_ev_short=-0.4)
    t0 = eng.get("EURUSD").long_thesis.last_refreshed_at
    d, ev, th = eng.get_best_thesis("EURUSD", now=t0)
    assert d == LONG and abs(ev - 0.6) < 1e-9 and th is not None
    d2, ev2, _ = eng.get_best_thesis("EURUSD", now=t0 + 100.0)
    assert d2 == LONG and abs(ev2 - 0.3) < 1e-6   # halved after one half-life


# ── 5. Decay-induced dominance flip ──────────────────────────────────────


def test_winner_flip_from_decay():
    eng = ThesisEngine(min_ev_threshold=0.1, decay_half_life=100.0)
    eng.update(
        "EURUSD",
        [_vote("structure", LONG, 0.9), _vote("momentum", SHORT, 0.5)],
        0.7, 0.3, entry_ev_long=0.5, entry_ev_short=0.12,
    )
    tset = eng.get("EURUSD")
    assert tset.dominant == LONG            # base (build-time) dominant

    # Simulate the LONG side having gone stale while SHORT stays fresh (an
    # independent refresh, as a future per-side feed would produce). LONG: 4
    # half-lives → 0.5 * 0.0625 = 0.03125; SHORT fresh → 0.12.
    now = tset.short_thesis.last_refreshed_at
    tset.long_thesis.last_refreshed_at = now - 400.0

    evs, dominant, _ = eng._decayed_view(tset, now)
    assert dominant == SHORT
    assert evs[LONG] < evs[SHORT]

    # should_act now points SHORT, and the flip is logged edge-triggered.
    act, direction, adv = eng.should_act("EURUSD", now=now)
    assert act is True and direction == SHORT
    assert eng.get("EURUSD").decay_flip_logged is True


# ── 6. Flat also decays (non-zero flat_ev) ───────────────────────────────


def test_flat_thesis_also_decays():
    eng = ThesisEngine(flat_ev=0.2, decay_half_life=100.0)
    eng.update("EURUSD", [_vote("structure", LONG, 0.5)], 0.5, 0.5,
               entry_ev_long=0.1, entry_ev_short=0.1)
    flat = eng.get("EURUSD").flat_thesis
    t0 = flat.last_refreshed_at
    assert abs(eng._effective_ev(flat, t0) - 0.2) < 1e-9
    assert abs(eng._effective_ev(flat, t0 + 100.0) - 0.1) < 1e-6   # halved


# ── 7. Master switch disables decay ──────────────────────────────────────


def test_decay_disabled_no_decay():
    eng = ThesisEngine(min_ev_threshold=0.3, decay_enabled=False,
                       decay_half_life=100.0)
    eng.update("EURUSD", [_vote("structure", LONG, 0.9)], 0.9, 0.1,
               entry_ev_long=0.6, entry_ev_short=-0.4)
    t0 = eng.get("EURUSD").long_thesis.last_refreshed_at
    # Even far in the future the thesis is undecayed → still actionable.
    act, direction, adv = eng.should_act("EURUSD", now=t0 + 10_000.0)
    assert act is True and direction == LONG
    assert abs(adv - 0.6) < 1e-9
    assert eng._time_decay_factor(eng.get("EURUSD").long_thesis,
                                  t0 + 10_000.0) == 1.0


# ── 8. Decay floor / expiry ──────────────────────────────────────────────


def test_decay_floor_marks_expired():
    eng = ThesisEngine(min_ev_threshold=0.1, decay_half_life=100.0,
                       decay_floor=0.05)
    eng.update("EURUSD", [_vote("structure", LONG, 0.9)], 0.9, 0.1,
               entry_ev_long=0.5, entry_ev_short=-0.4)
    tset = eng.get("EURUSD")
    t0 = tset.long_thesis.last_refreshed_at
    far = t0 + 500.0     # ~5 half-lives → 0.5 * 0.03125 = 0.015625 < floor
    assert eng._effective_ev(tset.long_thesis, far) < 0.05

    eng.challenge("EURUSD", now=far)
    assert eng.get("EURUSD").decay_expired_logged is True
    # An expired thesis is also below the action threshold.
    assert eng.get("EURUSD").decay_below_threshold_logged is True


# ── 9. Edge-triggered challenge flags reset on refresh ───────────────────


def test_challenge_flags_reset_on_update():
    eng = ThesisEngine(min_ev_threshold=0.3, decay_half_life=100.0)
    eng.update("EURUSD", [_vote("structure", LONG, 0.9)], 0.9, 0.1,
               entry_ev_long=0.5, entry_ev_short=-0.4)
    t0 = eng.get("EURUSD").long_thesis.last_refreshed_at
    eng.challenge("EURUSD", now=t0 + 300.0)     # decays below threshold
    assert eng.get("EURUSD").decay_below_threshold_logged is True

    # Fresh evidence rebuilds the ThesisSet → the edge flags reset.
    eng.update("EURUSD", [_vote("structure", LONG, 0.9)], 0.9, 0.1,
               entry_ev_long=0.5, entry_ev_short=-0.4)
    assert eng.get("EURUSD").decay_below_threshold_logged is False
    assert eng.get("EURUSD").decay_expired_logged is False
    assert eng.get("EURUSD").decay_flip_logged is False


def test_challenge_all_is_fail_safe_and_covers_all_symbols():
    eng = ThesisEngine(min_ev_threshold=0.3, decay_half_life=100.0)
    for sym in ("EURUSD", "GBPUSD"):
        eng.update(sym, [_vote("structure", LONG, 0.9)], 0.9, 0.1,
                   entry_ev_long=0.5, entry_ev_short=-0.4)
    t0 = eng.get("EURUSD").long_thesis.last_refreshed_at
    # No crash, and both stale symbols cross the threshold.
    eng.challenge_all(now=t0 + 400.0)
    assert eng.get("EURUSD").decay_below_threshold_logged is True
    assert eng.get("GBPUSD").decay_below_threshold_logged is True


# ── 10. get_status surfaces decay ────────────────────────────────────────


def test_get_status_exposes_decay():
    eng = ThesisEngine(decay_half_life=123.0, decay_floor=0.02)
    eng.update("EURUSD", [_vote("structure", LONG, 0.9)], 0.9, 0.1,
               entry_ev_long=0.6, entry_ev_short=-0.4)
    st = eng.get_status()
    assert st["decay_enabled"] is True
    assert st["decay_half_life"] == 123.0
    assert st["decay_floor"] == 0.02
    eff = st["theses"]["EURUSD"]["effective"]
    assert eff["dominant"] == LONG
    assert "long_ev" in eff and "short_ev" in eff and "flat_ev" in eff


# ── 11. Config guards + engine fallback ──────────────────────────────────


def test_engine_bad_half_life_falls_back():
    eng = ThesisEngine(decay_half_life=0.0)
    assert eng.decay_half_life == 900.0
    eng2 = ThesisEngine(decay_half_life=-5.0)
    assert eng2.decay_half_life == 900.0


def test_thesis_config_validation():
    from config import ThesisConfig

    ThesisConfig()  # defaults must construct
    with pytest.raises(ValueError):
        ThesisConfig(thesis_decay_half_life=0.0)
    with pytest.raises(ValueError):
        ThesisConfig(thesis_decay_half_life=-1.0)
    with pytest.raises(ValueError):
        ThesisConfig(thesis_decay_floor=-0.1)

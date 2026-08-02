"""Tests for the hardened five-check flip confirmation (entry/flip_confirmer.py).

Each check is exercised in isolation (with the others arranged to pass or skip)
and then in combination, plus per-instrument threshold overrides and graceful
degradation when a data source is unavailable. The existing stop-out flip and
A1 direction-flip regressions live in tests/test_stopout_flip.py and
tests/test_entry_orchestrator.py — this file covers the confirmer itself.
"""

from types import SimpleNamespace

import pandas as pd

from entry.flip_confirmer import FlipConfirmer


# ── DataFrame builders ─────────────────────────────────────────────────────
def _df(closes, vols, highs=None, lows=None):
    """Build an M1 frame from CLOSED-bar values, appending a forming bar.

    ``drop_forming_bar`` (used by the volume + clean-break checks) removes the
    last row, so the appended copy leaves exactly the ``closes`` / ``vols`` bars
    as the CLOSED history. The last element of ``closes`` is therefore the "last
    closed" candle the clean-break check reads.
    """
    highs = list(highs) if highs is not None else [c + 0.5 for c in closes]
    lows = list(lows) if lows is not None else [c - 0.5 for c in closes]
    closes2 = list(closes) + [closes[-1]]
    vols2 = list(vols) + [vols[-1]]
    highs2 = highs + [highs[-1]]
    lows2 = lows + [lows[-1]]
    return pd.DataFrame(
        {"open": closes2, "high": highs2, "low": lows2, "close": closes2, "tick_volume": vols2}
    )


def _rising_vol_df():
    """24 closed bars with rising volume — the volume check passes comfortably."""
    closes = [2000.0 + i * 0.1 for i in range(24)]
    vols = [100 + i * 5 for i in range(24)]
    return _df(closes, vols)


def _vol_df(closed_vols):
    """24 closed bars with the supplied volume pattern (flat closes)."""
    closes = [2000.0] * len(closed_vols)
    return _df(closes, closed_vols)


def _session_ctx(name):
    return SimpleNamespace(get_session=lambda: SimpleNamespace(value=name))


def _make_confirmer(
    *,
    atr_pips=10.0,
    move_pips=10.0,
    tick_eff=0.5,
    m5="UNKNOWN",
    m15="UNKNOWN",
    m1_df="__rising__",
    session=None,
    pip_size=0.01,
    profile_param=None,
    get_recent_ticks=None,
    sequence_tracker=None,
):
    """A FlipConfirmer whose every check passes by default; override to fail one."""
    if isinstance(m1_df, str) and m1_df == "__rising__":
        m1_df = _rising_vol_df()
    sess = _session_ctx(session) if session is not None else None
    return FlipConfirmer(
        get_atr_pips=lambda s, tf: atr_pips,
        get_tick_momentum=lambda s, d, p: tick_eff,
        get_tick_move_pips=lambda s, d, p: move_pips,
        get_structure_trend=lambda s, tf: {"M5": m5, "M15": m15}.get(tf, "UNKNOWN"),
        get_m1_dataframe=lambda s: m1_df,
        get_recent_ticks=get_recent_ticks,
        sequence_tracker=sequence_tracker,
        session_context=sess,
        pip_size_lookup=lambda s: pip_size,
        profile_param=profile_param,
    )


# ── Check 1: ATR-normalised magnitude ──────────────────────────────────────
class TestAtrMagnitude:
    def test_move_below_threshold_rejected(self):
        # move 2 / ATR 10 = 0.2 < 0.3 default.
        c = _make_confirmer(atr_pips=10.0, move_pips=2.0)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is False
        assert res.reason == "atr_move"
        assert res.checks["atr"] == "fail"

    def test_move_at_threshold_passes(self):
        # move 3 / ATR 10 = 0.3 == threshold → passes; the rest pass too.
        c = _make_confirmer(atr_pips=10.0, move_pips=3.0)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["atr"] == "pass"

    def test_unavailable_atr_skips(self):
        c = _make_confirmer(atr_pips=0.0, move_pips=0.0)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["atr"] == "skip"


# ── Check 2: session-aware tick efficiency ─────────────────────────────────
class TestTickEfficiencySession:
    def test_passes_in_london(self):
        # 0.35 ≥ 0.30 default (London / non-Asian). ATR skipped to isolate.
        c = _make_confirmer(atr_pips=0.0, tick_eff=0.35, session="LONDON")
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["tick"] == "pass"
        assert res.checks["tick_threshold"] == 0.30

    def test_rejected_in_asian(self):
        # 0.35 < 0.45 Asian threshold.
        c = _make_confirmer(atr_pips=0.0, tick_eff=0.35, session="ASIAN")
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is False
        assert res.reason == "tick_efficiency"
        assert res.checks["tick_threshold"] == 0.45

    def test_missing_session_uses_default_threshold(self):
        c = _make_confirmer(atr_pips=0.0, tick_eff=0.35, session=None)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["tick_threshold"] == 0.30


# ── Check 3: multi-TF structural non-opposition ────────────────────────────
class TestStructureNonOpposition:
    def test_m5_bearish_blocks_long(self):
        c = _make_confirmer(atr_pips=0.0, m5="BEARISH")
        res = c.confirm("XAUUSD", "LONG")
        assert res.confirmed is False
        assert res.reason == "m5_opposition"

    def test_m15_bearish_blocks_long(self):
        c = _make_confirmer(atr_pips=0.0, m5="RANGING", m15="BEARISH")
        res = c.confirm("XAUUSD", "LONG")
        assert res.confirmed is False
        assert res.reason == "m15_opposition"

    def test_m15_ranging_allows_long(self):
        c = _make_confirmer(atr_pips=0.0, m5="RANGING", m15="RANGING")
        res = c.confirm("XAUUSD", "LONG")
        assert res.confirmed is True
        assert res.checks["m15"] == "skip"

    def test_m15_check_off_by_profile(self):
        # flip_require_m15_non_opposition=False ⇒ an opposing M15 no longer blocks.
        pp = _pp({"flip_require_m15_non_opposition": False})
        c = _make_confirmer(atr_pips=0.0, m5="RANGING", m15="BEARISH", profile_param=pp)
        res = c.confirm("XAUUSD", "LONG")
        assert res.confirmed is True


# ── Check 4: volume confirmation ───────────────────────────────────────────
class TestVolume:
    def test_below_average_rejected(self):
        df = _vol_df([100] * 22 + [40, 40])
        c = _make_confirmer(atr_pips=0.0, m1_df=df)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is False
        assert res.reason == "volume"

    def test_above_average_passes(self):
        c = _make_confirmer(atr_pips=0.0, m1_df=_rising_vol_df())
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["volume"] == "pass"

    def test_asian_requires_higher_ratio(self):
        # ratio ≈ 1.13 — clears the 1.0 default but not the 1.3 Asian floor.
        df = _vol_df([100] * 22 + [115, 115])
        passes = _make_confirmer(atr_pips=0.0, m1_df=df, session="LONDON", tick_eff=0.5)
        assert passes.confirm("XAUUSD", "SHORT").confirmed is True
        blocked = _make_confirmer(atr_pips=0.0, m1_df=df, session="ASIAN", tick_eff=0.5)
        res = blocked.confirm("XAUUSD", "SHORT")
        assert res.confirmed is False
        assert res.reason == "volume"

    def test_missing_volume_column_skips(self):
        df = pd.DataFrame({"close": [2000.0 + i for i in range(25)]})
        c = _make_confirmer(atr_pips=0.0, m1_df=df)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["volume"] == "skip"


# ── Check 5: invalidation clean-break ──────────────────────────────────────
def _clean_df(last_close, last_high, last_low):
    closes = [2000.0] * 23 + [last_close]
    vols = [100 + i * 5 for i in range(24)]  # rising → volume passes
    highs = [2000.5] * 23 + [last_high]
    lows = [1999.5] * 23 + [last_low]
    return _df(closes, vols, highs, lows)


class TestCleanBreak:
    def test_close_beyond_level_passes(self):
        # LONG→SHORT flip: last close cleanly BELOW the invalidation level.
        df = _clean_df(last_close=1999.0, last_high=2000.2, last_low=1998.5)
        c = _make_confirmer(atr_pips=0.0, m1_df=df)
        res = c.confirm("XAUUSD", "SHORT", invalidation_level=2000.0)
        assert res.confirmed is True
        assert res.checks["clean_break"] == "pass"

    def test_wick_only_rejected(self):
        # Low pierces below the level but the candle closes back above it.
        df = _clean_df(last_close=2001.0, last_high=2001.5, last_low=1998.0)
        c = _make_confirmer(atr_pips=0.0, m1_df=df)
        res = c.confirm("XAUUSD", "SHORT", invalidation_level=2000.0)
        assert res.confirmed is False
        assert res.reason == "clean_break"

    def test_long_flip_requires_close_above(self):
        # SHORT→LONG flip: last close cleanly ABOVE the invalidation level.
        df = _clean_df(last_close=2001.0, last_high=2001.5, last_low=1999.5)
        c = _make_confirmer(atr_pips=0.0, m5="RANGING", m1_df=df)
        res = c.confirm("XAUUSD", "LONG", invalidation_level=2000.0)
        assert res.confirmed is True

    def test_zero_level_skips(self):
        c = _make_confirmer(atr_pips=0.0)
        res = c.confirm("XAUUSD", "SHORT", invalidation_level=0.0)
        assert res.confirmed is True
        assert res.checks["clean_break"] == "skip"

    def test_clean_break_off_by_profile(self):
        pp = _pp({"flip_require_clean_break": False})
        df = _clean_df(last_close=2001.0, last_high=2001.5, last_low=1998.0)
        c = _make_confirmer(atr_pips=0.0, m1_df=df, profile_param=pp)
        res = c.confirm("XAUUSD", "SHORT", invalidation_level=2000.0)
        assert res.confirmed is True


# ── Combined + specific-reason short-circuit ───────────────────────────────
class TestCombined:
    def test_all_five_pass_confirms(self):
        df = _clean_df(last_close=1999.0, last_high=2000.2, last_low=1998.5)
        c = _make_confirmer(atr_pips=10.0, move_pips=10.0, tick_eff=0.5, m1_df=df)
        res = c.confirm("XAUUSD", "SHORT", invalidation_level=2000.0)
        assert res.confirmed is True
        assert res.reason == "confirmed"
        assert res.checks["atr"] == "pass"
        assert res.checks["tick"] == "pass"
        assert res.checks["m5"] == "pass"
        assert res.checks["volume"] == "pass"
        assert res.checks["clean_break"] == "pass"

    def test_single_failure_short_circuits_with_reason(self):
        base = dict(atr_pips=10.0, move_pips=10.0, tick_eff=0.5)
        # Each override fails exactly one check.
        assert _make_confirmer(**{**base, "move_pips": 1.0}).confirm(
            "XAUUSD", "SHORT"
        ).reason == "atr_move"
        assert _make_confirmer(**{**base, "tick_eff": 0.1}).confirm(
            "XAUUSD", "SHORT"
        ).reason == "tick_efficiency"
        assert _make_confirmer(**{**base, "m5": "BULLISH"}).confirm(
            "XAUUSD", "SHORT"
        ).reason == "m5_opposition"
        assert _make_confirmer(
            **{**base, "m1_df": _vol_df([100] * 22 + [30, 30])}
        ).confirm("XAUUSD", "SHORT").reason == "volume"


# ── Per-instrument profile overrides ───────────────────────────────────────
def _pp(overrides):
    def resolve(symbol, name, default):
        return overrides.get(name, default)

    return resolve


class TestProfileOverrides:
    def test_atr_threshold_override_respected(self):
        # move 5 / ATR 10 = 0.5 — passes the 0.3 default but not an 0.8 override.
        default_c = _make_confirmer(atr_pips=10.0, move_pips=5.0)
        assert default_c.confirm("XAUUSD", "SHORT").confirmed is True
        strict = _make_confirmer(
            atr_pips=10.0, move_pips=5.0, profile_param=_pp({"flip_atr_move_threshold": 0.8})
        )
        res = strict.confirm("XAUUSD", "SHORT")
        assert res.confirmed is False
        assert res.reason == "atr_move"

    def test_tick_threshold_override_respected(self):
        strict = _make_confirmer(
            atr_pips=0.0, tick_eff=0.35,
            profile_param=_pp({"flip_tick_threshold_default": 0.60}),
        )
        res = strict.confirm("XAUUSD", "SHORT")
        assert res.confirmed is False
        assert res.reason == "tick_efficiency"


# ── Graceful degradation ───────────────────────────────────────────────────
class TestGracefulDegradation:
    def test_all_optional_sources_missing(self):
        # Only tick momentum wired; ATR / M1 / structure / session all absent.
        c = FlipConfirmer(get_tick_momentum=lambda s, d, p: 0.5)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["atr"] == "skip"
        assert res.checks["tick"] == "pass"
        assert res.checks["volume"] == "skip"
        assert res.checks["clean_break"] == "skip"

    def test_weak_ticks_still_fail_when_everything_else_missing(self):
        c = FlipConfirmer(get_tick_momentum=lambda s, d, p: 0.05)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is False
        assert res.reason == "tick_efficiency"

    def test_faulty_callable_fails_closed(self):
        def boom(*_a, **_k):
            raise RuntimeError("broker down")

        # A raising tick source is swallowed to 0.0 → weak-tick rejection, not a crash.
        c = FlipConfirmer(get_tick_momentum=boom)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is False


# ── Backtest-style per-check toggling ──────────────────────────────────────
class TestChecksEnabledToggling:
    def test_disabled_checks_are_skipped(self):
        # An opposing M5 + weak ticks would normally block; disabling those
        # checks lets the flip through (backtest independent-toggle behaviour).
        c = _make_confirmer(atr_pips=0.0, tick_eff=0.0, m5="BULLISH")
        enabled = {"atr": False, "tick": False, "m5": False, "m15": False,
                   "volume": False, "clean_break": False}
        res = c.confirm("XAUUSD", "SHORT", checks_enabled=enabled)
        assert res.confirmed is True


# ── Check 6: tick-rule order-flow delta ────────────────────────────────────
def _delta_ticks(prices, spread=0.01):
    return [SimpleNamespace(bid=p, ask=p + spread) for p in prices]


class TestDeltaCheck:
    def test_delta_blocks_flip_when_opposing(self):
        # LONG flip but recent ticks are all selling → delta rejects it.
        sell = _delta_ticks([2000.0 - i for i in range(15)])
        pp = _pp({"flip_require_delta_confirmation": True, "flip_delta_threshold": 0.2})
        c = _make_confirmer(
            atr_pips=0.0, m5="RANGING",
            get_recent_ticks=lambda s, n: sell, profile_param=pp,
        )
        res = c.confirm("XAUUSD", "LONG")
        assert res.confirmed is False
        assert res.reason == "delta"
        assert res.checks["delta"] == "fail"

    def test_delta_confirms_when_aligned(self):
        buy = _delta_ticks([2000.0 + i for i in range(15)])
        pp = _pp({"flip_require_delta_confirmation": True, "flip_delta_threshold": 0.2})
        c = _make_confirmer(
            atr_pips=0.0, get_recent_ticks=lambda s, n: buy, profile_param=pp,
        )
        res = c.confirm("XAUUSD", "LONG")
        assert res.confirmed is True
        assert res.checks["delta"] == "pass"

    def test_delta_skips_when_disabled(self):
        # Feature flag explicitly disabled via the profile → the delta check
        # never runs (the flag now defaults ON, so the test forces it off).
        sell = _delta_ticks([2000.0 - i for i in range(15)])
        pp = _pp({"flip_require_delta_confirmation": False})
        c = _make_confirmer(
            atr_pips=0.0, get_recent_ticks=lambda s, n: sell, profile_param=pp,
        )
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["delta"] == "skip"

    def test_delta_skips_on_insufficient_ticks(self):
        few = _delta_ticks([2000.0 - i for i in range(5)])  # < 10 usable ticks
        pp = _pp({"flip_require_delta_confirmation": True})
        c = _make_confirmer(
            atr_pips=0.0, get_recent_ticks=lambda s, n: few, profile_param=pp,
        )
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["delta"] == "skip"

    def test_delta_skips_when_no_tick_source(self):
        pp = _pp({"flip_require_delta_confirmation": True})
        c = _make_confirmer(atr_pips=0.0, get_recent_ticks=None, profile_param=pp)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["delta"] == "skip"


# ── Check 7: fast-then-slow temporal sequencing ────────────────────────────
class TestSequenceCheck:
    def test_sequence_blocks_when_not_fully_confirmed(self):
        tracker = SimpleNamespace(is_confirmed=lambda s, d: False)
        pp = _pp({"flip_require_sequence": True})
        c = _make_confirmer(atr_pips=0.0, sequence_tracker=tracker, profile_param=pp)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is False
        assert res.reason == "sequence"
        assert res.checks["sequence"] == "fail"

    def test_sequence_passes_when_fully_confirmed(self):
        tracker = SimpleNamespace(is_confirmed=lambda s, d: True)
        pp = _pp({"flip_require_sequence": True})
        c = _make_confirmer(atr_pips=0.0, sequence_tracker=tracker, profile_param=pp)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["sequence"] == "pass"

    def test_sequence_skips_when_no_tracker(self):
        pp = _pp({"flip_require_sequence": True})
        c = _make_confirmer(atr_pips=0.0, sequence_tracker=None, profile_param=pp)
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["sequence"] == "skip"


class TestCheck6And7SkipWhenDisabled:
    def test_both_skip_when_feature_flags_false(self):
        # With both flags explicitly disabled via the profile, neither gate
        # engages even with a rejecting tracker and opposing ticks wired. (The
        # flags now default ON, so the test forces them off to isolate the
        # skip-when-disabled behaviour.)
        sell = _delta_ticks([2000.0 - i for i in range(15)])
        tracker = SimpleNamespace(is_confirmed=lambda s, d: False)
        pp = _pp({
            "flip_require_delta_confirmation": False,
            "flip_require_sequence": False,
        })
        c = _make_confirmer(
            atr_pips=0.0,
            get_recent_ticks=lambda s, n: sell,
            sequence_tracker=tracker,
            profile_param=pp,
        )
        res = c.confirm("XAUUSD", "SHORT")
        assert res.confirmed is True
        assert res.checks["delta"] == "skip"
        assert res.checks["sequence"] == "skip"

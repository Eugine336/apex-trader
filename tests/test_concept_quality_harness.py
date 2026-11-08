"""Shared quality gates for every registered concept module.

* scale invariance — multiplying every price by a constant must not change any
  direction, trigger state or strength (levels scale with it). A failure means an
  absolute price threshold hides in the module (a "configured number").
* mirror symmetry — reflecting prices (p -> C - p) must swap LONG and SHORT with
  mirrored levels. A failure means a direction bias.
* time-shift invariance — shifting every timestamp by whole days changes nothing.
* purity — the same input gives the same reading.

Each check runs on a corpus of deterministic synthetic series and compares only the
readings a module actually emits; modules that emit nothing on the corpus are listed
by ``test_corpus_exercises_every_module`` so a dead module cannot pass silently.
"""

import math

import numpy as np
import pytest

from cognition.concepts import CONCEPT_MODULES

TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
EPOCH0 = 1_700_000_000 - (1_700_000_000 % 86400) - 400 * 86400   # a Wednesday-agnostic UTC midnight


def _scenario(kind, n_m5, seed):
    rng = np.random.default_rng(seed)
    base = 1.2000
    sigma = 0.00035
    steps = rng.normal(0, sigma, n_m5)
    if kind == "up":
        steps += 0.000006
    elif kind == "down":
        steps -= 0.000006
    elif kind == "range":
        steps = rng.normal(0, sigma, n_m5) - 0.02 * np.cumsum(rng.normal(0, sigma, n_m5)) * 0.0
    elif kind == "volatile":
        steps = rng.normal(0, sigma, n_m5) * (1 + 2 * (np.sin(np.arange(n_m5) / 900.0) > 0.7))
    elif kind == "reversal":
        half = n_m5 // 2
        steps[:half] += 0.000008
        steps[half:] -= 0.000008
    close = base + np.cumsum(steps)
    if kind == "range":
        close = base + 0.004 * np.sin(np.arange(n_m5) / 700.0) + np.cumsum(rng.normal(0, sigma * 0.15, n_m5))
    open_ = np.concatenate([[base], close[:-1]])
    wick_u = np.abs(rng.normal(0, sigma * 0.6, n_m5))
    wick_d = np.abs(rng.normal(0, sigma * 0.6, n_m5))
    high = np.maximum(open_, close) + wick_u
    low = np.minimum(open_, close) - wick_d
    vol = rng.integers(50, 300, n_m5).astype(float)
    t = EPOCH0 + np.arange(n_m5) * 300
    return t, open_, high, low, close, vol


def _frames(t, o, h, l, c, v, end):
    """Completed bars per timeframe using only M5 data up to index ``end`` (exclusive)."""
    out = {}
    for tf, sec in TF_SECONDS.items():
        k = sec // 300
        first = int(np.ceil(t[0] / sec) * sec)
        last_close = int(t[end - 1] + 300)
        starts = np.arange(first, last_close - sec + 1, sec)
        bars = []
        for s in starts[-120:]:
            idx = np.where((t[:end] >= s) & (t[:end] < s + sec))[0]
            if len(idx) != k:
                continue
            bars.append({"open": float(o[idx[0]]), "high": float(h[idx].max()), "low": float(l[idx].min()),
                         "close": float(c[idx[-1]]), "volume": float(v[idx].sum()),
                         "timestamp_epoch": float(s), "source_timestamp_verified": True})
        out[tf] = bars
    return out


def _corpus():
    n = 14_000
    series = []
    for kind, seed in (("up", 1), ("down", 2), ("range", 3), ("volatile", 4), ("reversal", 5), ("up", 6)):
        series.append((kind, _scenario(kind, n, seed)))
    cases = []
    for kind, (t, o, h, l, c, v) in series:
        for end in (n - 1, n - 13, n - 61, n - 305, n - 1201, n - 2999):
            cases.append((kind, end, _frames(t, o, h, l, c, v, end)))
    return cases


_CASES = None


def cases():
    global _CASES
    if _CASES is None:
        _CASES = _corpus()
    return _CASES


def _transform(frames, price=lambda p: p, flip=False, shift=0.0):
    out = {}
    for tf, bars in frames.items():
        rows = []
        for b in bars:
            o, h, l, c = (price(b[k]) for k in ("open", "high", "low", "close"))
            if flip:
                h, l = l, h
            rows.append({**b, "open": o, "high": h, "low": l, "close": c,
                         "timestamp_epoch": b["timestamp_epoch"] + shift})
        out[tf] = rows
    return out


def _read(module, frames):
    try:
        return module.read("EURUSD", frames)
    except Exception as exc:                      # a fault is a harness failure, reported with the module
        raise AssertionError(f"{module.key} raised {type(exc).__name__}: {exc}") from exc


def _sig(reading, scale=1.0, mirror=None):
    """Comparable signature of a reading: direction/trigger/strength plus scaled levels."""
    out = []
    for imp in reading.implications:
        d = imp.direction
        if mirror is not None and d in ("LONG", "SHORT"):
            d = "SHORT" if d == "LONG" else "LONG"

        def lvl(x):
            if x is None:
                return None
            return round(mirror - x if mirror is not None else x * scale, 7)

        out.append((d, bool(imp.activated), None if imp.strength is None else round(imp.strength, 4),
                    lvl(imp.invalidation_price), lvl(imp.objective_price)))
    return (reading.concept, reading.timeframe, tuple(sorted(out, key=repr)))


def _all(module, frames, **kw):
    return sorted((_sig(r, **({"scale": kw["scale"]} if "scale" in kw else {"mirror": kw["mirror"]} if "mirror" in kw else {}))
                   for r in _read(module, frames)), key=repr)


# Modules whose reading depends on information the synthetic OHLC corpus does not carry
# (cost facts, other modules' readings) are checked by their own tests instead.
NEEDS_OTHER_INPUTS = {"execution_cost", "strategy_lens_readings"}
# Calendar-position concepts change when the date changes; time-shift invariance does not apply.
CALENDAR_DEPENDENT = {"seasonality"}

# Known mirror-symmetry defects found by this harness (direction bias or direction-dependent
# discretisation). strict=True: when a module is fixed the test XPASSes and must be removed here.
KNOWN_MIRROR_DEFECTS = {
    "swing_geometry": "Fibonacci swing selection differs on a mirrored chart (different anchor swing).",
    "structure_patterns": "A chart pattern found on the original is not found on the mirrored chart (long/short detection asymmetric).",
    "volume_profile": "Value-area/POC levels depend on price-bin alignment anchored at the minimum price, so mirrored levels differ.",
    "seasonality": "Mirrored data changes the reading (check the signed tendency handling).",
    "range_rotation": "Objective level depends on the same direction-dependent price binning as volume_profile.",
    "regime_switching": "Trigger state (activated) differs on a mirrored chart.",
    "wyckoff_volume_readings": "Invalidation level differs on a mirrored chart (range-bound selection is direction-dependent).",
}


def _params(defects=None):
    out = []
    for m in CONCEPT_MODULES:
        marks = []
        if defects and m.key in defects:
            marks.append(pytest.mark.xfail(strict=True, reason=defects[m.key]))
        out.append(pytest.param(m, id=m.key, marks=marks))
    return out


MODULES = _params()
MIRROR_MODULES = _params(KNOWN_MIRROR_DEFECTS)


@pytest.mark.parametrize("module", MODULES)
def test_scale_invariance_no_absolute_price_thresholds(module):
    for kind, end, frames in cases():
        for factor in (1000.0, 0.001):
            expected = _all(module, frames, scale=factor)          # base readings, levels multiplied
            scaled = _all(module, _transform(frames, price=lambda p, f=factor: p * f), scale=1.0)
            assert scaled == expected, f"{module.key}: readings change with price scale x{factor} on {kind}@{end}"


@pytest.mark.parametrize("module", MIRROR_MODULES)
def test_mirror_symmetry_no_direction_bias(module):
    c = 3.0
    for kind, end, frames in cases():
        base = _all(module, frames, scale=1.0)
        mirrored_frames = _transform(frames, price=lambda p: c - p, flip=True)
        mirrored = _all(module, mirrored_frames, scale=1.0)
        expect = sorted((_sig_mirror(r, c) for r in _read(module, frames)), key=repr)
        assert mirrored == expect, f"{module.key}: reflecting prices does not mirror the reading on {kind}@{end}"


def _sig_mirror(reading, c):
    return _sig(reading, mirror=c)


@pytest.mark.parametrize("module", MODULES)
def test_time_shift_and_purity(module):
    for kind, end, frames in cases()[:12]:
        a = _all(module, frames, scale=1.0)
        assert a == _all(module, frames, scale=1.0), f"{module.key}: not deterministic"
        if module.key in CALENDAR_DEPENDENT:
            continue
        shifted = _all(module, _transform(frames, shift=7 * 86400.0), scale=1.0)
        assert a == shifted, f"{module.key}: readings change when timestamps shift by whole days"


def test_corpus_exercises_every_module():
    silent = []
    for module in CONCEPT_MODULES:
        if module.key in NEEDS_OTHER_INPUTS:
            continue
        if not any(_read(module, frames) for _, _, frames in cases()):
            silent.append(module.key)
    # Informational guard: a module that never fires on the whole corpus is untested by this harness.
    assert not silent, f"modules that emit nothing on the quality corpus: {silent}"

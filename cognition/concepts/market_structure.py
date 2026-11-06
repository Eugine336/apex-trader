"""Market structure — the full reading (swings, trend state, BOS, CHoCH, internal vs external).

The concept: price moves in swings. A **swing high/low** is a local extreme confirmed
by the bars on both sides (a three-bar fractal). Swings of swings form the
**external** structure; the fractal swings themselves are the **internal** structure.
The sequence of external swings defines the trend state: higher highs with higher
lows is an uptrend, lower highs with lower lows a downtrend, anything else a range
or transition. A **break of structure (BOS)** is a completed close beyond the last
external swing in the direction of the trend (continuation). A **change of
character (CHoCH)** is the first completed close beyond the swing that was holding
the trend (the last higher low in an uptrend, the last lower high in a downtrend),
which is the earliest structural sign the trend may be turning. The decisiveness of
the break bar is measured against this instrument's own candles (displacement).

The module reads that on each timeframe and returns:

* the trend state and the last external swings (the levels that matter);
* whether a break occurred on the latest completed bar, and which kind;
* the implication for each side, the invalidation (the swing that must hold), an
  objective only where earlier structure actually supplies one, what is still
  unconfirmed, and what each state means for managing a position;
* whether the signal agrees or disagrees with the higher timeframes it can see.

Nothing is invented: where history supplies no objective, the reading says so.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from functools import lru_cache
from typing import Optional

from cognition.contracts import EvidenceDomain
from cognition.concepts.base import (
    FLAT, LONG, SHORT, TIMEFRAME_SECONDS, ConceptReading, Implication, finite,
    mean_known, percentile_rank, reference_price,
)


def _clean(bars: list[dict]) -> list[dict]:
    out = []
    for b in bars or []:
        o, h, l, c = (finite(b.get(k)) for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c) or h < l:
            continue
        out.append({**b, "open": o, "high": h, "low": l, "close": c})
    return out


@lru_cache(maxsize=512)
def _fractal_swings_cached(
    values: tuple[tuple[float, float], ...],
) -> tuple[tuple[int, str, float], ...]:
    """Shared immutable swing result keyed by the exact high/low history."""
    raw: list[tuple[int, str, float]] = []
    for i in range(1, len(values) - 1):
        previous_high, previous_low = values[i - 1]
        high, low = values[i]
        following_high, following_low = values[i + 1]
        if high > previous_high and high >= following_high:
            raw.append((i, "H", high))
        if low < previous_low and low <= following_low:
            raw.append((i, "L", low))
    raw.sort(key=lambda item: (item[0], item[1]))
    out: list[tuple[int, str, float]] = []
    for swing in raw:
        if out and out[-1][1] == swing[1]:
            keep_new = (
                swing[2] > out[-1][2] if swing[1] == "H"
                else swing[2] < out[-1][2]
            )
            if keep_new:
                out[-1] = swing
            continue
        out.append(swing)
    return tuple(out)


_ROLLING_SWING_CACHE: OrderedDict[
    str, tuple[tuple[tuple[float, float], ...], tuple[tuple[int, str, float], ...]]
] = OrderedDict()
_ROLLING_SWING_CACHE_LOCK = threading.RLock()
_ROLLING_SWING_CACHE_MAX_ENTRIES = 256
_ROLLING_SWING_STATS = {
    "exact_hits": 0, "prefix_hits": 0, "incremental_updates": 0, "full_rebuilds": 0,
}


def _append_confirmed_pivot(
    values: tuple[tuple[float, float], ...],
    pivots: tuple[tuple[int, str, float], ...],
) -> tuple[tuple[int, str, float], ...]:
    if len(values) < 3:
        return ()
    index = len(values) - 2
    previous_high, previous_low = values[index - 1]
    high, low = values[index]
    following_high, following_low = values[index + 1]
    new = []
    if high > previous_high and high >= following_high:
        new.append((index, "H", high))
    if low < previous_low and low <= following_low:
        new.append((index, "L", low))
    out = list(pivots)
    for swing in new:
        if out and out[-1][1] == swing[1]:
            more_extreme = (
                swing[2] > out[-1][2] if swing[1] == "H"
                else swing[2] < out[-1][2]
            )
            if more_extreme:
                out[-1] = swing
        else:
            out.append(swing)
    return tuple(out)


def fractal_swing_cache_status() -> dict[str, int]:
    with _ROLLING_SWING_CACHE_LOCK:
        return {**_ROLLING_SWING_STATS, "entries": len(_ROLLING_SWING_CACHE)}


def clear_fractal_swing_cache() -> None:
    with _ROLLING_SWING_CACHE_LOCK:
        _ROLLING_SWING_CACHE.clear()
        for key in _ROLLING_SWING_STATS:
            _ROLLING_SWING_STATS[key] = 0
    _fractal_swings_cached.cache_clear()


def fractal_swings(
    bars: list[dict], *, cache_key: str = "",
) -> list[tuple[int, str, float]]:
    """Confirmed fractal swings with append/rolling-window incremental updates.

    The optional cache key scopes a series to one symbol/timeframe. If a new
    completed bar is appended or a fixed-size history advances by one bar, only
    the newly confirmable right-edge pivot is evaluated. Any gap or historical
    correction triggers a full rebuild. Callers without a stable scope use the
    bounded exact-history cache.
    """
    values = tuple(
        (float(bar["high"]), float(bar["low"]))
        for bar in bars
    )
    if not cache_key:
        return list(_fractal_swings_cached(values))

    key = str(cache_key)
    with _ROLLING_SWING_CACHE_LOCK:
        previous = _ROLLING_SWING_CACHE.get(key)
        if previous is not None:
            previous_values, previous_pivots = previous
            if values == previous_values:
                _ROLLING_SWING_CACHE.move_to_end(key)
                _ROLLING_SWING_STATS["exact_hits"] += 1
                return list(previous_pivots)
            if (
                len(values) + 1 == len(previous_values)
                and values == previous_values[:-1]
            ):
                # A downstream concept may need the same history without the
                # latest bar (pool-before-sweep semantics). Return that prefix
                # view without replacing the full-series state used next cycle.
                prefix_pivots = tuple(
                    swing for swing in previous_pivots
                    if swing[0] < len(values) - 1
                )
                _ROLLING_SWING_CACHE.move_to_end(key)
                _ROLLING_SWING_STATS["prefix_hits"] += 1
                return list(prefix_pivots)
            if len(values) == len(previous_values) + 1 and values[:-1] == previous_values:
                pivots = _append_confirmed_pivot(values, previous_pivots)
                _ROLLING_SWING_STATS["incremental_updates"] += 1
            elif len(values) == len(previous_values) and previous_values[1:] == values[:-1]:
                # The source returns a fixed rolling window. Drop pivots whose
                # left-hand confirmation bar has left the window, shift indices,
                # then evaluate the newly confirmable right-edge pivot.
                shifted = tuple(
                    (index - 1, kind, price)
                    for index, kind, price in previous_pivots
                    if index > 1
                )
                pivots = _append_confirmed_pivot(values, shifted)
                _ROLLING_SWING_STATS["incremental_updates"] += 1
            else:
                pivots = _fractal_swings_cached(values)
                _ROLLING_SWING_STATS["full_rebuilds"] += 1
        else:
            pivots = _fractal_swings_cached(values)
            _ROLLING_SWING_STATS["full_rebuilds"] += 1
        _ROLLING_SWING_CACHE[key] = (values, pivots)
        _ROLLING_SWING_CACHE.move_to_end(key)
        while len(_ROLLING_SWING_CACHE) > _ROLLING_SWING_CACHE_MAX_ENTRIES:
            _ROLLING_SWING_CACHE.popitem(last=False)
        return list(pivots)


@lru_cache(maxsize=512)
def _external_swings_cached(
    internal: tuple[tuple[int, str, float], ...],
) -> tuple[tuple[int, str, float], ...]:
    highs = [s for s in internal if s[1] == "H"]
    lows = [s for s in internal if s[1] == "L"]
    out = []
    for seq, kind in ((highs, "H"), (lows, "L")):
        for j in range(1, len(seq) - 1):
            a, b, c = seq[j - 1][2], seq[j][2], seq[j + 1][2]
            if (kind == "H" and b > a and b >= c) or (kind == "L" and b < a and b <= c):
                out.append(seq[j])
    out.sort(key=lambda item: item[0])
    return tuple(out)


def external_swings(internal: list[tuple[int, str, float]]) -> list[tuple[int, str, float]]:
    """Cached swings-of-swings calculation over immutable confirmed pivots."""
    return list(_external_swings_cached(tuple(internal)))


def trend_state(ext: list[tuple[int, str, float]]) -> tuple[str, dict]:
    highs = [s for s in ext if s[1] == "H"]
    lows = [s for s in ext if s[1] == "L"]
    if len(highs) < 2 or len(lows) < 2:
        return "undetermined", {}
    hh = highs[-1][2] > highs[-2][2]
    hl = lows[-1][2] > lows[-2][2]
    lh = highs[-1][2] < highs[-2][2]
    ll = lows[-1][2] < lows[-2][2]
    levels = {"last_high": highs[-1][2], "prev_high": highs[-2][2],
              "last_low": lows[-1][2], "prev_low": lows[-2][2],
              "last_high_idx": highs[-1][0], "last_low_idx": lows[-1][0]}
    if hh and hl:
        return "up", levels
    if lh and ll:
        return "down", levels
    return "range_or_transition", levels


def _displacement(bar: dict, history: list[dict]) -> Optional[float]:
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return None
    body = abs(bar["close"] - bar["open"]) / rng
    sample = [abs(b["close"] - b["open"]) / (b["high"] - b["low"]) for b in history if b["high"] > b["low"]]
    body_pct = percentile_rank(body, sample)
    ranges = [b["high"] - b["low"] for b in history]
    range_pct = percentile_rank(rng, ranges)
    return mean_known([body_pct, range_pct])


class MarketStructure:
    key = "market_structure"
    family = "structure"
    covers_methods = ("market_structure", "internal_external_structure")
    covers_lenses: tuple[str, ...] = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        ref_close, ref_tf = reference_price(bars_by_tf)
        analysed: dict[str, tuple] = {}
        for tf, raw in bars_by_tf.items():
            if tf not in TIMEFRAME_SECONDS or TIMEFRAME_SECONDS[tf] < 300:
                continue
            bars = _clean(raw)
            if len(bars) < 8:
                continue
            internal = fractal_swings(bars, cache_key=f"market-swings:{symbol}:{tf}")
            ext = external_swings(internal)
            state, levels = trend_state(ext)
            order = "external"
            swings = ext
            if state == "undetermined":      # a clean staircase has no swing-of-swings yet
                state, levels = trend_state(internal)
                order, swings = "internal", internal
            analysed[tf] = (bars, internal, swings, state, levels, order)
        trends = {tf: v[3] for tf, v in analysed.items()}
        readings = []
        for tf, (bars, internal, swings, state, levels, order) in analysed.items():
            if state == "undetermined":
                continue
            readings.append(self._reading(symbol, tf, bars, internal, swings, state, levels, order,
                                          trends, ref_close, ref_tf))
        return readings

    # ------------------------------------------------------------------
    def _reading(self, symbol, tf, bars, internal, ext, state, lv, order, trends, ref_close, ref_tf):
        last, prev = bars[-1], bars[-2]
        # Swings confirmed strictly before the latest bar (the structure the latest bar can break).
        usable_ext = [s for s in ext if s[0] < len(bars) - 1]
        highs = [s for s in usable_ext if s[1] == "H"]
        lows = [s for s in usable_ext if s[1] == "L"]
        epoch = finite(last.get("timestamp_epoch")) if last.get("source_timestamp_verified") is True else None
        last_h, last_l = (highs[-1][2] if highs else None), (lows[-1][2] if lows else None)

        event = None
        if last_h is not None and last["close"] > last_h and prev["close"] <= last_h:
            event = "bos_up" if state == "up" else "choch_up" if state == "down" else "break_up_in_range"
        elif last_l is not None and last["close"] < last_l and prev["close"] >= last_l:
            event = "bos_down" if state == "down" else "choch_down" if state == "up" else "break_down_in_range"

        displacement = _displacement(last, bars[:-1]) if event else None
        higher = [t for t in trends if TIMEFRAME_SECONDS.get(t, 0) > TIMEFRAME_SECONDS[tf]]
        htf = {t: trends[t] for t in higher}

        def aligned(side: str) -> tuple[list[str], list[str]]:
            want = "up" if side == LONG else "down"
            agree = [f"{t} trend {s}" for t, s in htf.items() if s == want]
            against = [f"counter to {t} trend {s}" for t, s in htf.items() if s in ("up", "down") and s != want]
            return agree, against

        implications: list[Implication] = []

        # An objective is only ever an earlier structural level that price has not
        # yet taken out: the most recent swing beyond price in the trade direction.
        def lower_highs_above(price: float) -> Optional[float]:
            above = [s for s in ext if s[1] == "H" and s[2] > price]
            return max(above, key=lambda s: s[0])[2] if above else None

        def lower_lows_below(price: float) -> Optional[float]:
            below = [s for s in ext if s[1] == "L" and s[2] < price]
            return max(below, key=lambda s: s[0])[2] if below else None

        if state == "up":
            hl = lv["last_low"]
            obj = lower_highs_above(last["close"])
            agree, against = aligned(LONG)
            implications.append(Implication(
                direction=LONG,
                statement=(f"{tf} {order} structure is an uptrend (higher highs, higher lows); the higher low "
                           f"{hl:.10g} is the level that must hold."
                           + (" A break of structure just printed." if event == "bos_up" else "")),
                strength=displacement if event == "bos_up" else None,
                activated=event == "bos_up",
                activation_reason=(f"completed close above the last external high {last_h:.10g} in an uptrend"
                                   if event == "bos_up" else ""),
                invalidation_price=hl,
                invalidation_reason=(f"completed close below the last higher low {hl:.10g} on {tf} is a change of "
                                     "character and ends the uptrend"),
                objective_price=obj,
                requires=(() if obj is not None else
                          ("an objective: no earlier external swing high above price; needs a measured move or a "
                           "higher-timeframe reference",)),
                conflicts_with=tuple(against),
                management={"invalid_on_completed_close_beyond": hl, "trail_anchor": "each new higher low",
                            "timeframe": tf},
            ))
        elif state == "down":
            lh = lv["last_high"]
            obj = lower_lows_below(last["close"])
            agree, against = aligned(SHORT)
            implications.append(Implication(
                direction=SHORT,
                statement=(f"{tf} {order} structure is a downtrend (lower highs, lower lows); the lower high "
                           f"{lh:.10g} is the level that must hold."
                           + (" A break of structure just printed." if event == "bos_down" else "")),
                strength=displacement if event == "bos_down" else None,
                activated=event == "bos_down",
                activation_reason=(f"completed close below the last external low {last_l:.10g} in a downtrend"
                                   if event == "bos_down" else ""),
                invalidation_price=lh,
                invalidation_reason=(f"completed close above the last lower high {lh:.10g} on {tf} is a change of "
                                     "character and ends the downtrend"),
                objective_price=obj,
                requires=(() if obj is not None else
                          ("an objective: no earlier external swing low below price; needs a measured move or a "
                           "higher-timeframe reference",)),
                conflicts_with=tuple(against),
                management={"invalid_on_completed_close_beyond": lh, "trail_anchor": "each new lower high",
                            "timeframe": tf},
            ))
        else:
            implications.append(Implication(
                direction=FLAT, strength=None, activated=False, activation_reason="",
                statement=(f"{tf} {order} structure is a range or transition (mixed highs and lows); no "
                           "directional structure to trade with."),
                requires=("a completed close beyond the range's external swings to show which side is accepted",),
            ))

        # The reversal reading when the trend was just challenged.
        if event == "choch_down":
            stop = lv["last_high"]
            obj = lower_lows_below(last["close"])
            _, against = aligned(SHORT)
            implications.append(Implication(
                direction=SHORT,
                statement=(f"Change of character on {tf}: a completed close below the last higher low {last_l:.10g} "
                           "is the first structural sign the uptrend may be ending."),
                strength=displacement, activated=True,
                activation_reason=f"completed close below the last higher low {last_l:.10g} (uptrend challenged)",
                invalidation_price=stop,
                invalidation_reason=(f"completed close above the last external high {stop:.10g} on {tf} restores "
                                     "the uptrend"),
                objective_price=obj,
                requires=tuple(["confirmation that lower highs form (a second lower swing)"]
                               + ([] if obj is not None else ["an objective: no earlier external swing low below price"])),
                conflicts_with=tuple(against),
                management={"invalid_on_completed_close_beyond": stop, "timeframe": tf},
            ))
        elif event == "choch_up":
            stop = lv["last_low"]
            obj = lower_highs_above(last["close"])
            _, against = aligned(LONG)
            implications.append(Implication(
                direction=LONG,
                statement=(f"Change of character on {tf}: a completed close above the last lower high {last_h:.10g} "
                           "is the first structural sign the downtrend may be ending."),
                strength=displacement, activated=True,
                activation_reason=f"completed close above the last lower high {last_h:.10g} (downtrend challenged)",
                invalidation_price=stop,
                invalidation_reason=(f"completed close below the last external low {stop:.10g} on {tf} restores "
                                     "the downtrend"),
                objective_price=obj,
                requires=tuple(["confirmation that higher lows form (a second higher swing)"]
                               + ([] if obj is not None else ["an objective: no earlier external swing high above price"])),
                conflicts_with=tuple(against),
                management={"invalid_on_completed_close_beyond": stop, "timeframe": tf},
            ))

        facts = {
            "trend_state": state, "structure_order": order, "event": event, "displacement_percentile": displacement,
            "last_external_high": last_h, "last_external_low": last_l,
            "external_swings": [(i, k, p) for i, k, p in usable_ext[-6:]],
            "internal_swing_count": len(internal), "higher_timeframe_trends": htf,
            "history_candles": len(bars),
        }
        obs = (f"{tf} {order} structure: {state}" + (f"; event {event}" if event else "")
               + f"; last external high {last_h if last_h is not None else 'n/a'}, "
                 f"last external low {last_l if last_l is not None else 'n/a'}.")
        return ConceptReading(
            concept=self.key, timeframe=tf, domain=EvidenceDomain.STRUCTURE, applicable=True,
            applicability_reason="enough completed bars to confirm external swings on both sides",
            observation=obs, implications=tuple(implications), facts=facts,source_bar_epoch=epoch,
 source_timestamp_verified=epoch is not None,
            reference_close=ref_close, reference_timeframe=ref_tf,
            unknowns=tuple(sorted({r for i in implications for r in i.requires})),
        )

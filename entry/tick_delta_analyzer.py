"""APEX TRADER — Tick-rule order flow delta (flip confirmation Check 6).

Computes an aggressor delta from stored tick bid/ask data — the strongest
momentum-WITH-participation signal extractable from MT5's data model without a
true L2 orderbook. It is the sixth gate of the hardened flip confirmation
(entry/flip_confirmer.py): a flip should only fire when order flow is actually
pushing in the flip direction, not merely when price has drifted there.

The classic tick rule (Lee-Ready style, simplified) classifies each trade by
comparing its price to the previous one: an up-tick is aggressive buying, a
down-tick is aggressive selling. MT5 streams bid/ask (and sometimes a ``last``
trade price) rather than signed trades, so this analyzer reconstructs the same
signal from the mid price (or ``last`` when the broker provides it):

    * Buy-aggressor  — the reference price moved UP  (toward the ask).
    * Sell-aggressor — the reference price moved DOWN (toward the bid).
    * Neutral        — no change.

The delta ratio ``(buy - sell) / (buy + sell)`` lives in ``[-1, +1]``: +1 is
all buying, -1 all selling, 0 balanced. A LONG flip requires the ratio to clear
``+threshold`` (net buying pressure); a SHORT flip requires it to reach
``-threshold`` (net selling pressure).

The analyzer is STATELESS — it receives a list of recent ticks (the same shape
as ``TickStore.get_recent(symbol, count)`` returns) and returns a
:class:`DeltaResult`. It degrades gracefully: fewer than ``min_ticks`` usable
ticks yields an ``insufficient_ticks`` result (the confirmer treats that as a
SKIP, never a block) and any malformed tick is silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence

# A meaningful aggressor delta needs a minimum sample of ticks; below this the
# ratio is dominated by noise, so the check degrades to a graceful skip.
MIN_TICKS = 10


@dataclass(frozen=True)
class DeltaResult:
    """Outcome of a tick-rule order-flow delta computation.

    ``confirmed`` is meaningful only for :meth:`TickDeltaAnalyzer.confirm`
    (direction-aware); :meth:`TickDeltaAnalyzer.analyze` always returns
    ``confirmed=False`` and leaves the verdict to the caller. ``reason`` names
    the outcome — ``"ok"`` / ``"confirmed"`` / ``"delta_below_threshold"`` /
    ``"insufficient_ticks"`` / ``"unknown_direction"`` — so the confirmer can
    distinguish a genuine rejection (block) from a data gap (skip).
    """

    confirmed: bool
    delta_ratio: float
    buy_count: int
    sell_count: int
    neutral_count: int
    reason: str


class TickDeltaAnalyzer:
    """Stateless tick-rule aggressor-delta computer.

    Construct once (optionally with a custom minimum tick count) and call
    :meth:`analyze` for the raw delta or :meth:`confirm` for the direction-aware
    flip verdict. Safe to share across threads (holds no mutable state).
    """

    def __init__(self, min_ticks: Optional[int] = None) -> None:
        # Floor at 2 (a delta needs at least one consecutive pair); default to
        # the module-level MIN_TICKS so the "meaningful sample" contract holds.
        base = MIN_TICKS if min_ticks is None else int(min_ticks)
        self._min_ticks = max(2, base)

    @property
    def min_ticks(self) -> int:
        return self._min_ticks

    # ── Public API ────────────────────────────────────────────────────
    def analyze(self, ticks: Optional[Sequence[Any]]) -> DeltaResult:
        """Compute the raw (direction-agnostic) aggressor delta over ``ticks``.

        Extracts a reference price per tick (``last`` when present and positive,
        otherwise the bid/ask mid), classifies each consecutive pair as
        buy/sell/neutral by the tick rule, and returns the delta ratio in
        ``[-1, +1]``. Fewer than ``min_ticks`` usable prices → an
        ``insufficient_ticks`` result. ``confirmed`` is always ``False`` here.
        """
        prices = self._reference_prices(ticks)
        n = len(prices)
        if n < self._min_ticks:
            return DeltaResult(False, 0.0, 0, 0, 0, "insufficient_ticks")

        buy = sell = neutral = 0
        for i in range(1, n):
            diff = prices[i] - prices[i - 1]
            if diff > 0:
                buy += 1
            elif diff < 0:
                sell += 1
            else:
                neutral += 1

        total = buy + sell
        delta_ratio = (buy - sell) / total if total > 0 else 0.0
        return DeltaResult(False, delta_ratio, buy, sell, neutral, "ok")

    def confirm(
        self,
        ticks: Optional[Sequence[Any]],
        direction: str,
        threshold: float = 0.2,
    ) -> DeltaResult:
        """Confirm a flip ``direction`` against the aggressor delta.

        LONG requires ``delta_ratio >= +threshold`` (net buying pressure);
        SHORT requires ``delta_ratio <= -threshold`` (net selling pressure).
        Returns the :class:`DeltaResult` from :meth:`analyze` with ``confirmed``
        / ``reason`` set. An ``insufficient_ticks`` analysis is passed straight
        through (the confirmer skips it); an unrecognised direction yields
        ``unknown_direction`` (also a skip).
        """
        base = self.analyze(ticks)
        if base.reason == "insufficient_ticks":
            return base

        want = str(direction or "").upper()
        thr = abs(float(threshold))
        if want == "LONG":
            confirmed = base.delta_ratio >= thr
        elif want == "SHORT":
            confirmed = base.delta_ratio <= -thr
        else:
            return replace(base, confirmed=False, reason="unknown_direction")

        reason = "confirmed" if confirmed else "delta_below_threshold"
        return replace(base, confirmed=confirmed, reason=reason)

    # ── Internals ─────────────────────────────────────────────────────
    def _reference_prices(self, ticks: Optional[Sequence[Any]]) -> list[float]:
        """Extract a clean list of reference prices from raw ticks.

        Prefers a positive ``last`` trade price when the broker supplies one,
        otherwise the bid/ask mid. Malformed / non-positive ticks are skipped so
        one bad read never corrupts the delta.
        """
        prices: list[float] = []
        for tick in ticks or []:
            price = self._reference_price(tick)
            if price is not None and price > 0:
                prices.append(price)
        return prices

    @classmethod
    def _reference_price(cls, tick: Any) -> Optional[float]:
        """Reference price for a single tick: ``last`` if usable, else the mid."""
        last = cls._read(tick, "last")
        val = cls._to_float(last)
        if val is not None and val > 0:
            return val

        bid = cls._to_float(cls._read(tick, "bid"))
        ask = cls._to_float(cls._read(tick, "ask"))
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (bid + ask) / 2.0

        # Fall back to a ``mid`` attribute/key when bid/ask are unavailable.
        mid = cls._to_float(cls._read(tick, "mid"))
        if mid is not None and mid > 0:
            return mid
        return None

    @staticmethod
    def _read(tick: Any, key: str) -> Any:
        """Read ``key`` from a tick supplied as an object or a mapping."""
        if isinstance(tick, dict):
            return tick.get(key)
        return getattr(tick, key, None)

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

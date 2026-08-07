"""Synthetic market-data generators for testing the backtest harness.

Each generator returns a list of valid :class:`~backtest.data.Candle` rows
(strictly increasing timestamps, OHLC-consistent) driven by a seeded RNG so
output is reproducible. These scenarios let the engine and tests run without any
recorded data and exercise the regime detector across distinct market states.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from backtest.data import Candle, validate_candles

_TF_MINUTES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440,
}


def _step(timeframe: str) -> timedelta:
    return timedelta(minutes=_TF_MINUTES.get(timeframe.upper(), 5))


def _bar_from_open_close(
    t: datetime, o: float, c: float, wick: float, rng: random.Random
) -> Candle:
    """Build an OHLC-consistent bar from an open/close plus a wick magnitude."""
    hi = max(o, c) + abs(rng.gauss(0, wick))
    lo = min(o, c) - abs(rng.gauss(0, wick))
    lo = max(lo, 1e-9)
    vol = round(abs(rng.gauss(1000, 250)), 1)
    return Candle(time=t, open=o, high=hi, low=lo, close=c, volume=vol)


def _series(
    closes: list[float],
    *,
    pair: str,
    timeframe: str,
    start: datetime,
    wick: float,
    rng: random.Random,
    validate: bool,
) -> list[Candle]:
    step = _step(timeframe)
    candles: list[Candle] = []
    t = start
    prev = closes[0]
    for c in closes:
        candles.append(_bar_from_open_close(t, prev, c, wick, rng))
        prev = c
        t = t + step
    return validate_candles(candles, pair=pair) if validate else candles


def _start_time() -> datetime:
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def trending(
    *, candles: int = 500, pair: str = "EURUSD", timeframe: str = "M5",
    base_price: float = 1.10, drift: float = 0.00004, volatility: float = 0.00015,
    seed: int = 42, validate: bool = True, down: bool = False,
) -> list[Candle]:
    """A directional market with pullbacks (up by default, down if ``down``)."""
    rng = random.Random(seed)
    sign = -1.0 if down else 1.0
    price = base_price
    closes: list[float] = []
    for _ in range(candles):
        price += sign * drift + rng.gauss(0, volatility)
        price = max(price, base_price * 0.5)
        closes.append(price)
    return _series(
        closes, pair=pair, timeframe=timeframe, start=_start_time(),
        wick=volatility, rng=rng, validate=validate,
    )


def ranging(
    *, candles: int = 500, pair: str = "EURUSD", timeframe: str = "M5",
    base_price: float = 1.10, amplitude: float = 0.0015, period: int = 60,
    volatility: float = 0.0001, seed: int = 42, validate: bool = True,
) -> list[Candle]:
    """An oscillating market between support and resistance."""
    rng = random.Random(seed)
    closes: list[float] = []
    for i in range(candles):
        price = base_price + amplitude * math.sin(2 * math.pi * i / max(2, period))
        price += rng.gauss(0, volatility)
        closes.append(price)
    return _series(
        closes, pair=pair, timeframe=timeframe, start=_start_time(),
        wick=volatility, rng=rng, validate=validate,
    )


def volatile(
    *, candles: int = 500, pair: str = "EURUSD", timeframe: str = "M5",
    base_price: float = 1.10, volatility: float = 0.0009, seed: int = 42,
    validate: bool = True,
) -> list[Candle]:
    """Large candles and whipsaws — high volatility, no clear direction."""
    rng = random.Random(seed)
    price = base_price
    closes: list[float] = []
    for _ in range(candles):
        price += rng.gauss(0, volatility)
        price = max(price, base_price * 0.5)
        closes.append(price)
    return _series(
        closes, pair=pair, timeframe=timeframe, start=_start_time(),
        wick=volatility * 1.5, rng=rng, validate=validate,
    )


def quiet(
    *, candles: int = 500, pair: str = "EURUSD", timeframe: str = "M5",
    base_price: float = 1.10, volatility: float = 0.00002, seed: int = 42,
    validate: bool = True,
) -> list[Candle]:
    """Small candles and low range — a quiet, low-ATR market."""
    rng = random.Random(seed)
    price = base_price
    closes: list[float] = []
    for _ in range(candles):
        price += rng.gauss(0, volatility)
        closes.append(price)
    return _series(
        closes, pair=pair, timeframe=timeframe, start=_start_time(),
        wick=volatility, rng=rng, validate=validate,
    )


def regime_transitions(
    *, candles: int = 800, segment: Optional[int] = None, pair: str = "EURUSD",
    timeframe: str = "M5", base_price: float = 1.10, seed: int = 42,
    validate: bool = True,
) -> list[Candle]:
    """Quiet → trending up → ranging → volatile, stitched into one continuous
    series (each segment starts where the previous one ended). ``candles`` is the
    total length; it is split evenly across the four segments."""
    rng = random.Random(seed)
    seg = int(segment) if segment else max(1, candles // 4)
    closes: list[float] = []
    price = base_price

    def extend(fn_closes: list[float]) -> None:
        nonlocal price
        # Re-anchor the segment so the series stays continuous.
        offset = price - fn_closes[0]
        for c in fn_closes:
            closes.append(c + offset)
        price = closes[-1]

    extend([c.close for c in quiet(
        candles=seg, base_price=price, seed=seed, validate=False)])
    extend([c.close for c in trending(
        candles=seg, base_price=price, seed=seed + 1, validate=False)])
    extend([c.close for c in ranging(
        candles=seg, base_price=price, seed=seed + 2, validate=False)])
    extend([c.close for c in volatile(
        candles=seg, base_price=price, seed=seed + 3, validate=False)])
    return _series(
        closes, pair=pair, timeframe=timeframe, start=_start_time(),
        wick=0.0002, rng=rng, validate=validate,
    )


def flash_crash(
    *, candles: int = 300, pair: str = "EURUSD", timeframe: str = "M5",
    base_price: float = 1.10, crash_at: float = 0.5, crash_pct: float = 0.05,
    volatility: float = 0.0001, seed: int = 42, validate: bool = True,
) -> list[Candle]:
    """A sudden spike down at ``crash_at`` (fraction of the series) then recovery."""
    rng = random.Random(seed)
    crash_idx = int(candles * crash_at)
    price = base_price
    closes: list[float] = []
    for i in range(candles):
        if i == crash_idx:
            price *= (1.0 - crash_pct)
        elif crash_idx < i < crash_idx + 20:
            price += (base_price - price) * 0.1  # gradual recovery
        else:
            price += rng.gauss(0, volatility)
        price = max(price, base_price * 0.5)
        closes.append(price)
    return _series(
        closes, pair=pair, timeframe=timeframe, start=_start_time(),
        wick=volatility, rng=rng, validate=validate,
    )


def trend_reversal(
    *, candles: int = 500, pair: str = "EURUSD", timeframe: str = "M5",
    base_price: float = 1.10, drift: float = 0.00004, volatility: float = 0.00015,
    seed: int = 42, validate: bool = True,
) -> list[Candle]:
    """A slow transition from uptrend to downtrend (drift flips at the midpoint)."""
    rng = random.Random(seed)
    half = candles // 2
    price = base_price
    closes: list[float] = []
    for i in range(candles):
        sign = 1.0 if i < half else -1.0
        price += sign * drift + rng.gauss(0, volatility)
        price = max(price, base_price * 0.5)
        closes.append(price)
    return _series(
        closes, pair=pair, timeframe=timeframe, start=_start_time(),
        wick=volatility, rng=rng, validate=validate,
    )


# Registry for the CLI's ``--synthetic <name>`` flag.
GENERATORS = {
    "trending": trending,
    "trending_down": lambda **kw: trending(down=True, **kw),
    "ranging": ranging,
    "volatile": volatile,
    "quiet": quiet,
    "regime_transitions": regime_transitions,
    "flash_crash": flash_crash,
    "trend_reversal": trend_reversal,
}


def generate(name: str, **kwargs) -> list[Candle]:
    """Generate a named scenario. Raises KeyError for unknown names."""
    if name not in GENERATORS:
        raise KeyError(
            f"unknown synthetic scenario {name!r}; choose from {sorted(GENERATORS)}"
        )
    return GENERATORS[name](**kwargs)

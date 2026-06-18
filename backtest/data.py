"""Backtest data layer — Candle model, loading and validation.

Loads historical OHLCV from CSV or JSON into immutable :class:`Candle` rows and
validates them (monotonic timestamps, no duplicate bars, OHLC sanity). Pure
stdlib parsing (the ``csv`` / ``json`` modules) — no pandas dependency for
loading; pandas is only used at the broker's ``get_ohlcv`` interface boundary.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


class DataValidationError(ValueError):
    """Raised when historical data fails a structural sanity check."""


@dataclass(frozen=True)
class Candle:
    """A single OHLCV bar. ``time`` is a timezone-aware UTC datetime."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def as_row(self) -> dict:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


_CANONICAL_FIELDS = ("time", "open", "high", "low", "close", "volume")

# Accept common column-name aliases so real exports load without massaging.
_ALIASES = {
    "time": ("time", "timestamp", "date", "datetime", "t"),
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c", "price"),
    "volume": ("volume", "vol", "v", "tick_volume"),
}


def _parse_timestamp(raw) -> datetime:
    """Parse a timestamp from epoch seconds/millis or an ISO-8601 string."""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    # Numeric epoch (seconds or milliseconds).
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return _epoch_to_dt(float(raw))
    s = str(raw).strip()
    if not s:
        raise DataValidationError("empty timestamp")
    # Pure number as string → epoch.
    try:
        return _epoch_to_dt(float(s))
    except ValueError:
        pass
    # ISO-8601 (tolerate a trailing 'Z').
    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise DataValidationError(f"unparseable timestamp: {raw!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _epoch_to_dt(value: float) -> datetime:
    # Heuristic: values above ~1e11 are milliseconds, not seconds.
    if value > 1e11:
        value /= 1000.0
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _resolve_columns(header: Iterable[str]) -> dict[str, str]:
    """Map canonical field → actual header name using the alias table."""
    lower = {h.lower().strip(): h for h in header}
    resolved: dict[str, str] = {}
    for field, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in lower:
                resolved[field] = lower[alias]
                break
    missing = [f for f in ("time", "open", "high", "low", "close") if f not in resolved]
    if missing:
        raise DataValidationError(
            f"missing required column(s): {missing} (have: {list(header)})"
        )
    return resolved


def _coerce_candle(row: dict, cols: dict[str, str]) -> Candle:
    def num(field: str, default: Optional[float] = None) -> float:
        key = cols.get(field)
        if key is None or key not in row or row[key] in (None, ""):
            if default is not None:
                return default
            raise DataValidationError(f"missing value for {field}")
        try:
            return float(row[key])
        except (TypeError, ValueError) as exc:
            raise DataValidationError(f"non-numeric {field}: {row[key]!r}") from exc

    return Candle(
        time=_parse_timestamp(row[cols["time"]]),
        open=num("open"),
        high=num("high"),
        low=num("low"),
        close=num("close"),
        volume=num("volume", 0.0),
    )


def validate_candles(candles: list[Candle], *, pair: str = "") -> list[Candle]:
    """Validate structural integrity. Returns the list unchanged on success.

    Checks: non-empty, strictly increasing timestamps (no duplicates / no
    out-of-order bars), and OHLC sanity (high is the max, low is the min, all
    finite and positive).
    """
    label = f" for {pair}" if pair else ""
    if not candles:
        raise DataValidationError(f"no candles{label}")
    prev_time: Optional[datetime] = None
    for i, c in enumerate(candles):
        for name, val in (
            ("open", c.open), ("high", c.high),
            ("low", c.low), ("close", c.close),
        ):
            if not math.isfinite(val) or val <= 0:
                raise DataValidationError(
                    f"candle {i}{label}: {name}={val!r} not finite/positive"
                )
        if c.high < max(c.open, c.close) or c.high < c.low:
            raise DataValidationError(
                f"candle {i}{label}: high {c.high} below open/close/low"
            )
        if c.low > min(c.open, c.close) or c.low > c.high:
            raise DataValidationError(
                f"candle {i}{label}: low {c.low} above open/close/high"
            )
        if prev_time is not None and c.time <= prev_time:
            raise DataValidationError(
                f"candle {i}{label}: timestamp {c.time} not after {prev_time} "
                "(gaps are allowed, but bars must be strictly increasing)"
            )
        prev_time = c.time
    return candles


class HistoricalDataLoader:
    """Loads OHLCV history into validated :class:`Candle` lists.

    Supports CSV (header row with time/open/high/low/close[/volume], aliases
    accepted) and JSON (an array of candle objects). Multiple pairs are loaded
    independently. Loaded data is always validated unless ``validate=False``.
    """

    def load_csv(self, path: str | Path, *, pair: str = "", validate: bool = True) -> list[Candle]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"CSV not found: {p}")
        with p.open("r", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise DataValidationError(f"empty CSV: {p}")
            cols = _resolve_columns(reader.fieldnames)
            candles = [_coerce_candle(row, cols) for row in reader]
        candles.sort(key=lambda c: c.time)
        return validate_candles(candles, pair=pair) if validate else candles

    def load_json(self, path: str | Path, *, pair: str = "", validate: bool = True) -> list[Candle]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"JSON not found: {p}")
        with p.open("r") as f:
            payload = json.load(f)
        return self.load_records(payload, pair=pair, validate=validate)

    def load_records(self, payload, *, pair: str = "", validate: bool = True) -> list[Candle]:
        """Load from an already-parsed list of dict rows (or {'candles': [...]})."""
        if isinstance(payload, dict):
            payload = payload.get("candles") or payload.get("data") or []
        if not isinstance(payload, list):
            raise DataValidationError("JSON payload is not a list of candles")
        if not payload:
            return validate_candles([], pair=pair) if validate else []
        cols = _resolve_columns(payload[0].keys())
        candles = [_coerce_candle(row, cols) for row in payload]
        candles.sort(key=lambda c: c.time)
        return validate_candles(candles, pair=pair) if validate else candles

    def load(self, path: str | Path, *, pair: str = "", validate: bool = True) -> list[Candle]:
        """Auto-detect format from the file suffix."""
        suffix = Path(path).suffix.lower()
        if suffix == ".json":
            return self.load_json(path, pair=pair, validate=validate)
        return self.load_csv(path, pair=pair, validate=validate)

"""
APEX TRADER — Spread Monitor
When the spread widens, someone is about to get hurt.
We don't trade in wide spreads. Period.

v2 — Time-bucketed learning: spreads are tracked per (symbol, day_of_week, hour)
so the monitor knows what XAUUSD spread looks like at 8:05 AM Monday specifically,
not just a flat average that mixes London open spikes with quiet afternoon trading.

Cold-start: until a bucket has MIN_BUCKET_SAMPLES readings, it falls back to the
flat average across all buckets, then to the registry hardcoded fallback.
"""

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

from loguru import logger

from config import INSTRUMENT_REGISTRY


HISTORY_SIZE = 200          # flat rolling history (fallback + status)
BUCKET_WINDOW_MINUTES = 30  # each bucket covers a 30-min window
MIN_BUCKET_SAMPLES = 5      # readings needed before a bucket is trusted


@dataclass
class SpreadReading:
    spread_pips: float
    timestamp: datetime


def _bucket(ts: datetime) -> tuple[int, int]:
    """Return (day_of_week 0=Mon, bucket_index) for a timestamp.
    Each bucket covers BUCKET_WINDOW_MINUTES minutes of the hour.
    e.g. 08:05 → bucket 1 (08:00-08:30), 08:35 → bucket 3 (08:30-09:00)
    """
    buckets_per_hour = 60 // BUCKET_WINDOW_MINUTES
    bucket_idx = ts.hour * buckets_per_hour + ts.minute // BUCKET_WINDOW_MINUTES
    return ts.weekday(), bucket_idx


class SpreadMonitor:
    """
    Tracks spread history per instrument and blocks entries when spreads
    are abnormally wide.

    Two layers of history:
    1. Flat rolling deque (HISTORY_SIZE) — used for overall average and
       status reporting. Always populated.
    2. Time-bucketed dict keyed by (day_of_week, bucket_index) — each
       bucket accumulates readings for that specific 30-min window of the
       week. Once a bucket has MIN_BUCKET_SAMPLES readings it becomes the
       authoritative typical spread for that time slot, replacing the flat
       average. After one week of running every slot is calibrated.
    """

    def __init__(self, max_multiplier: float = 3.0):
        self.max_multiplier = max_multiplier
        # Flat history per symbol
        self._history: dict[str, deque[SpreadReading]] = {}
        # Time-bucketed history: symbol → {(dow, bucket): deque}
        self._buckets: dict[str, dict[tuple[int, int], deque[float]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=50))
        )
        self._lock = Lock()

    def record_spread(
        self,
        pair: str,
        spread_pips: float,
        timestamp: Optional[datetime] = None,
    ) -> None:
        pair = pair.upper()
        timestamp = timestamp or datetime.now(timezone.utc)
        key = _bucket(timestamp)
        with self._lock:
            # Flat history
            if pair not in self._history:
                self._history[pair] = deque(maxlen=HISTORY_SIZE)
            self._history[pair].append(SpreadReading(spread_pips=spread_pips, timestamp=timestamp))
            # Bucketed history
            self._buckets[pair][key].append(spread_pips)

    def get_typical_spread(
        self,
        pair: str,
        at_time: Optional[datetime] = None,
    ) -> float:
        """Return the typical spread for this symbol at this time of week.

        Priority:
        1. Time-bucket average for (day_of_week, 30-min window) if >= MIN_BUCKET_SAMPLES
        2. Flat rolling average across all readings
        3. Registry hardcoded fallback
        """
        pair = pair.upper()
        ts = at_time or datetime.now(timezone.utc)
        key = _bucket(ts)

        with self._lock:
            # 1. Try time bucket
            bucket_readings = self._buckets.get(pair, {}).get(key)
            if bucket_readings and len(bucket_readings) >= MIN_BUCKET_SAMPLES:
                avg = sum(bucket_readings) / len(bucket_readings)
                logger.debug(
                    "[SpreadMonitor] {} bucket ({},{}) → {:.1f} pips (n={})",
                    pair, key[0], key[1], avg, len(bucket_readings),
                )
                return round(avg, 2)

            # 2. Flat rolling average
            flat = self._history.get(pair)
            if flat and len(flat) > 0:
                avg = sum(r.spread_pips for r in flat) / len(flat)
                logger.debug(
                    "[SpreadMonitor] {} flat avg → {:.1f} pips (bucket cold, n={})",
                    pair, avg, len(flat),
                )
                return round(avg, 2)

        # 3. Registry fallback
        info = INSTRUMENT_REGISTRY.get(pair)
        if info is not None:
            return info.typical_spread_pips

        return 0.0

    # Keep old name as alias — all existing callers work unchanged
    def get_average_spread(self, pair: str) -> float:
        return self.get_typical_spread(pair)

    # Also expose as typical_spread for risk_engine.py line 503
    def typical_spread(self, pair: str) -> float:
        return self.get_typical_spread(pair)

    def is_spread_safe(
        self,
        pair: str,
        current_spread_pips: float,
        max_multiplier: Optional[float] = None,
        at_time: Optional[datetime] = None,
    ) -> tuple[bool, str]:
        pair = pair.upper()
        mult = max_multiplier if max_multiplier is not None else self.max_multiplier
        typical = self.get_typical_spread(pair, at_time=at_time)

        if typical <= 0:
            return True, "No spread history — allowing trade"

        if current_spread_pips > typical * mult:
            reason = (
                f"Spread {current_spread_pips:.1f} pips is "
                f"{current_spread_pips / typical:.1f}x the typical "
                f"{typical:.1f} — too wide"
            )
            logger.warning(f"[SpreadMonitor] {pair}: {reason}")
            return False, reason

        return True, f"Spread {current_spread_pips:.1f} within safe range (typical {typical:.1f})"

    def get_spread_status(self, pair: str, current_spread: float) -> str:
        typical = self.get_typical_spread(pair.upper())
        if typical <= 0:
            return "UNKNOWN"

        ratio = current_spread / typical
        if ratio < 0.7:
            return "TIGHT"
        if ratio <= 1.5:
            return "NORMAL"
        if ratio <= 3.0:
            return "WIDE"
        return "DANGEROUS"

    def bucket_coverage(self, pair: str) -> dict:
        """Diagnostic: how many time buckets have enough data for this symbol."""
        pair = pair.upper()
        with self._lock:
            buckets = self._buckets.get(pair, {})
            total = len(buckets)
            calibrated = sum(1 for b in buckets.values() if len(b) >= MIN_BUCKET_SAMPLES)
        # Full week = 7 days × (24h × 60min / 30min) = 336 buckets
        return {
            "symbol": pair,
            "total_buckets_seen": total,
            "calibrated_buckets": calibrated,
            "full_week_buckets": 336,
            "coverage_pct": round(calibrated / 336 * 100, 1),
        }

"""
APEX TRADER — Spread Monitor
When the spread widens, someone is about to get hurt.
We don't trade in wide spreads. Period.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from loguru import logger

from config import INSTRUMENT_REGISTRY


HISTORY_SIZE = 100


@dataclass
class SpreadReading:
    spread_pips: float
    timestamp: datetime


class SpreadMonitor:
    """
    Tracks spread history per instrument and blocks entries when spreads
    are abnormally wide — a reliable signal that market conditions are
    dangerous or the broker is hunting.
    """

    def __init__(self, max_multiplier: float = 3.0):
        self.max_multiplier = max_multiplier
        self._history: dict[str, deque[SpreadReading]] = {}
        # Scan threads append while the entry/heat paths read — serialise both.
        self._lock = Lock()

    def record_spread(
        self,
        pair: str,
        spread_pips: float,
        timestamp: datetime | None = None,
    ) -> None:
        pair = pair.upper()
        timestamp = timestamp or datetime.now(timezone.utc)
        with self._lock:
            if pair not in self._history:
                self._history[pair] = deque(maxlen=HISTORY_SIZE)
            self._history[pair].append(SpreadReading(spread_pips=spread_pips, timestamp=timestamp))

    def is_spread_safe(
        self,
        pair: str,
        current_spread_pips: float,
        max_multiplier: float | None = None,
    ) -> tuple[bool, str]:
        pair = pair.upper()
        mult = max_multiplier if max_multiplier is not None else self.max_multiplier
        avg = self.get_average_spread(pair)

        if avg <= 0:
            return True, "No spread history — allowing trade"

        if current_spread_pips > avg * mult:
            reason = (
                f"Spread {current_spread_pips:.1f} pips is {current_spread_pips / avg:.1f}x "
                f"the average {avg:.1f} — too wide"
            )
            logger.warning(f"[SpreadMonitor] {pair}: {reason}")
            return False, reason

        return True, f"Spread {current_spread_pips:.1f} within safe range (avg {avg:.1f})"

    def get_average_spread(self, pair: str) -> float:
        pair = pair.upper()
        with self._lock:
            readings = self._history.get(pair)
            if readings and len(readings) > 0:
                return sum(r.spread_pips for r in readings) / len(readings)

        info = INSTRUMENT_REGISTRY.get(pair)
        if info is not None:
            return info.typical_spread_pips

        return 0.0

    def get_spread_status(self, pair: str, current_spread: float) -> str:
        avg = self.get_average_spread(pair.upper())
        if avg <= 0:
            return "UNKNOWN"

        ratio = current_spread / avg
        if ratio < 0.7:
            return "TIGHT"
        if ratio <= 1.5:
            return "NORMAL"
        if ratio <= 3.0:
            return "WIDE"
        return "DANGEROUS"

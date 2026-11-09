"""Live MT5 trade-server clock bridge.

MT5 quote timestamps can be broker-server wall-clock epochs. The terminal does
not expose its server UTC offset through the Python API. A companion MQL5 EA
publishes TimeTradeServer()-TimeGMT() to the terminal's MQL5/Files directory.
This module accepts only a fresh, server-matched, internally consistent sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Mapping


CLOCK_FILENAME = "apex_mt5_clock_offset.txt"
CLOCK_MAX_AGE_SECONDS = 20.0
MIN_OFFSET_SECONDS = -12 * 60 * 60
MAX_OFFSET_SECONDS = 14 * 60 * 60


@dataclass(frozen=True)
class MT5ServerClockState:
    server: str
    offset_seconds: int
    observed_server_epoch: int
    observed_gmt_epoch: int
    file_age_seconds: float

    def server_now_epoch(self, now_epoch: float | None = None) -> float:
        now = time.time() if now_epoch is None else float(now_epoch)
        return float(self.observed_server_epoch) + (now - float(self.observed_gmt_epoch))

    def to_utc_epoch(self, server_epoch: float) -> float:
        return float(server_epoch) - float(self.offset_seconds)

    def age_seconds(self, server_epoch: float, now_epoch: float | None = None) -> float:
        return self.server_now_epoch(now_epoch) - float(server_epoch)


def _parse_key_values(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            values[key.strip()] = value.strip()
    return values


def load_mt5_server_clock(
    data_path: str | Path | None,
    expected_server: str | None,
    *,
    now_epoch: float | None = None,
    max_age_seconds: float = CLOCK_MAX_AGE_SECONDS,
) -> MT5ServerClockState | None:
    """Load a fresh offset emitted by the EA; return None for every ambiguity."""
    if not data_path or not expected_server:
        return None
    now = time.time() if now_epoch is None else float(now_epoch)
    path = Path(data_path) / "MQL5" / "Files" / CLOCK_FILENAME
    try:
        stat = path.stat()
        file_age = now - stat.st_mtime
        if file_age < -2.0 or file_age > max(1.0, float(max_age_seconds)):
            return None
        values = _parse_key_values(path.read_text(encoding="utf-8"))
        if values.get("schema_version") != "1":
            return None
        server = values.get("server", "")
        if server != str(expected_server):
            return None
        offset = int(values["offset_seconds"])
        server_epoch = int(values["observed_server_epoch"])
        gmt_epoch = int(values["observed_gmt_epoch"])
        if not (MIN_OFFSET_SECONDS <= offset <= MAX_OFFSET_SECONDS):
            return None
        if server_epoch <= 0 or gmt_epoch <= 0:
            return None
        if server_epoch - gmt_epoch != offset:
            return None
        # TimeGMT() and Python time.time() are derived from the same host clock.
        # This check rejects stale or mismatched samples; it does not claim that
        # the host clock is externally synchronized.
        if abs(now - gmt_epoch) > max(5.0, float(max_age_seconds) + 2.0):
            return None
        return MT5ServerClockState(
            server=server,
            offset_seconds=offset,
            observed_server_epoch=server_epoch,
            observed_gmt_epoch=gmt_epoch,
            file_age_seconds=max(0.0, file_age),
        )
    except (OSError, KeyError, TypeError, ValueError, OverflowError):
        return None


__all__ = [
    "CLOCK_FILENAME",
    "CLOCK_MAX_AGE_SECONDS",
    "MT5ServerClockState",
    "load_mt5_server_clock",
]

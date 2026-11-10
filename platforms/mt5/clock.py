"""Live MT5 trade-server clock bridge.

The Python MT5 API does not expose a direct TimeTradeServer() value. The
production synchronizer estimates the broker timestamp offset from an advancing
terminal tick and independent network UTC observations. It anchors the result
to monotonic time, carries an explicit uncertainty bound, and rejects stale or
inconsistent evidence. A chart-attached MQL5 publisher is not required.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import secrets
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from itertools import combinations
from pathlib import Path
from typing import Iterable, Mapping
from urllib.request import Request, urlopen


CLOCK_FILENAME = "apex_mt5_clock_offset.txt"
CLOCK_MAX_AGE_SECONDS = 20.0
MIN_OFFSET_SECONDS = -12 * 60 * 60
MAX_OFFSET_SECONDS = 14 * 60 * 60


@dataclass(frozen=True)
class MT5ServerClockState:
    server: str
    offset_seconds: float
    observed_server_epoch: int
    observed_gmt_epoch: int
    file_age_seconds: float
    observed_monotonic: float | None = None
    uncertainty_seconds: float = 0.0
    sources: tuple[str, ...] = ()

    def server_now_epoch(self, now_epoch: float | None = None) -> float:
        if now_epoch is None and self.observed_monotonic is not None:
            return float(self.observed_server_epoch) + (time.monotonic() - self.observed_monotonic)
        now = time.time() if now_epoch is None else float(now_epoch)
        return float(self.observed_server_epoch) + (now - float(self.observed_gmt_epoch))

    def to_utc_epoch(self, server_epoch: float) -> float:
        return float(server_epoch) - float(self.offset_seconds)

    def age_seconds(self, server_epoch: float, now_epoch: float | None = None) -> float:
        return self.server_now_epoch(now_epoch) + self.uncertainty_seconds - float(server_epoch)


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


# Automatic synchronization constants. NTP is preferred; authenticated HTTPS
# Date headers provide a fallback on networks that block outbound UDP/123.
NTP_SERVERS = ("time.cloudflare.com", "time.google.com", "pool.ntp.org")
HTTPS_TIME_URLS = (
    "https://www.google.com/generate_204",
    "https://www.cloudflare.com/cdn-cgi/trace",
)
NTP_EPOCH_DELTA = 2_208_988_800
NTP_TIMEOUT_SECONDS = 0.65
CLOCK_MAX_UNCERTAINTY_SECONDS = 2.5
NTP_MIN_SOURCES = 2


@dataclass(frozen=True)
class NTPClockSample:
    utc_epoch: float
    monotonic_epoch: float
    uncertainty_seconds: float
    server: str


def _query_ntp(server: str) -> NTPClockSample | None:
    nonce = secrets.token_bytes(8)
    packet = bytearray(48)
    packet[0] = 0x23
    packet[40:48] = nonce
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(NTP_TIMEOUT_SECONDS)
            before = time.monotonic()
            sock.sendto(packet, (server, 123))
            response, _ = sock.recvfrom(512)
            after = time.monotonic()
        if len(response) < 48:
            return None
        leap, mode, stratum = (response[0] >> 6) & 3, response[0] & 7, response[1]
        if leap == 3 or mode != 4 or not 1 <= stratum <= 15 or response[24:32] != nonce:
            return None
        seconds, fraction = struct.unpack("!II", response[40:48])
        if seconds == 0:
            return None
        rtt = after - before
        tx_utc = seconds - NTP_EPOCH_DELTA + fraction / 2**32
        return NTPClockSample(
            tx_utc - rtt / 2.0, (before + after) / 2.0,
            rtt / 2.0 + 0.55, f"ntp:{server}",
        )
    except (OSError, ValueError, struct.error):
        return None


def _query_https_time(url: str) -> NTPClockSample | None:
    try:
        before = time.monotonic()
        request = Request(url, method="HEAD", headers={"User-Agent": "ApexTrader-clock-sync/1"})
        with urlopen(request, timeout=1.0) as response:
            date_header = response.headers.get("Date")
        after = time.monotonic()
        if not date_header:
            return None
        utc_epoch = parsedate_to_datetime(date_header).timestamp()
        rtt = after - before
        return NTPClockSample(
            utc_epoch - rtt / 2.0, (before + after) / 2.0,
            rtt / 2.0 + 0.80, f"https:{url.split('/')[2]}",
        )
    except Exception:
        return None


def query_ntp_sources() -> tuple[NTPClockSample, ...]:
    """Collect independent network UTC observations without host wall time."""
    jobs = [(_query_ntp, host) for host in NTP_SERVERS]
    with ThreadPoolExecutor(max_workers=5) as pool:
        ntp = tuple(x for x in pool.map(lambda pair: pair[0](pair[1]), jobs) if x is not None)
        # When fewer than three NTP servers answer, collect HTTPS Date
        # observations as additional independent witnesses. Two NTP replies
        # alone cannot identify which one is the outlier if they disagree.
        if len(ntp) >= 3:
            return ntp
        https_jobs = [(_query_https_time, url) for url in HTTPS_TIME_URLS]
        https = tuple(x for x in pool.map(lambda pair: pair[0](pair[1]), https_jobs) if x is not None)
    return ntp + https


def estimate_mt5_server_clock(
    server: str,
    tick_server_epoch: float,
    tick_observed_monotonic: float,
    *,
    ntp_samples: Iterable[NTPClockSample] | None = None,
    tick_precision_seconds: float = 0.5,
    tick_transport_uncertainty_seconds: float = 1.0,
) -> MT5ServerClockState | None:
    """Estimate broker offset using a uniquely corroborated UTC sample cluster."""
    if not str(server or "").strip():
        return None
    try:
        tick_epoch = float(tick_server_epoch)
        tick_mono = float(tick_observed_monotonic)
        tick_precision = max(0.0, float(tick_precision_seconds))
        tick_transport_uncertainty = max(0.0, float(tick_transport_uncertainty_seconds))
    except (TypeError, ValueError, OverflowError):
        return None
    if tick_epoch <= 0:
        return None

    raw_samples = tuple(query_ntp_sources() if ntp_samples is None else ntp_samples)
    # Count one observation per clock operator. NTP and HTTPS endpoints
    # operated by the same provider are not independent witnesses.
    by_authority: dict[str, NTPClockSample] = {}
    for sample in raw_samples:
        endpoint = str(sample.server or "").split(":", 1)[-1].split("/", 1)[0]
        endpoint = endpoint.split(":", 1)[0].strip().lower()
        if not endpoint:
            continue
        authority = next(
            (
                domain for domain in ("cloudflare.com", "google.com", "ntp.org")
                if endpoint == domain or endpoint.endswith("." + domain)
            ),
            endpoint,
        )
        current = by_authority.get(authority)
        try:
            sample_quality = float(sample.uncertainty_seconds)
            current_quality = (
                float(current.uncertainty_seconds) if current is not None else float("inf")
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            math.isfinite(sample_quality)
            and sample_quality >= 0
            and sample_quality < current_quality
        ):
            by_authority[authority] = sample
    samples = tuple(by_authority.values())
    if len(samples) < NTP_MIN_SOURCES:
        return None

    estimates = []
    for sample in samples:
        try:
            value = tick_epoch - (
                float(sample.utc_epoch) + tick_mono - float(sample.monotonic_epoch)
            )
            sample_uncertainty = float(sample.uncertainty_seconds)
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            not math.isfinite(value)
            or not math.isfinite(sample_uncertainty)
            or sample_uncertainty < 0
        ):
            continue
        estimates.append((value, sample, sample_uncertainty))
    if len(estimates) < NTP_MIN_SOURCES:
        return None

    # Build source clusters whose uncertainty intervals have a common
    # intersection. A single slow or misbehaving NTP endpoint is excluded only
    # when the remaining independent sources uniquely corroborate one another.
    clusters = []
    for size in range(len(estimates), NTP_MIN_SOURCES - 1, -1):
        for cluster in combinations(estimates, size):
            lower = max(value - uncertainty for value, _, uncertainty in cluster)
            upper = min(value + uncertainty for value, _, uncertainty in cluster)
            if lower <= upper:
                clusters.append(cluster)
        if clusters:
            break
    if not clusters:
        return None

    # Multiple equally large but materially different clusters are ambiguous.
    # Do not choose whichever source happens to sort first.
    cluster_medians = []
    for cluster in clusters:
        values = sorted(value for value, _, _ in cluster)
        mid = len(values) // 2
        median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0
        cluster_medians.append(median)
    if max(cluster_medians) - min(cluster_medians) > 0.25:
        return None

    selected = clusters[0]
    ordered = sorted(value for value, _, _ in selected)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0
    spread = max(abs(value - median) for value in ordered)
    uncertainty = (
        max(sample_uncertainty for _, _, sample_uncertainty in selected)
        + spread + tick_precision / 2.0
        + tick_transport_uncertainty + 0.15
    )
    if not (MIN_OFFSET_SECONDS <= median <= MAX_OFFSET_SECONDS):
        return None
    if uncertainty > CLOCK_MAX_UNCERTAINTY_SECONDS:
        return None

    return MT5ServerClockState(
        server=str(server),
        offset_seconds=median,
        observed_server_epoch=tick_epoch,
        observed_gmt_epoch=tick_epoch - median,
        file_age_seconds=0.0,
        observed_monotonic=tick_mono,
        uncertainty_seconds=uncertainty,
        sources=tuple(sample.server for _, sample, _ in selected),
    )

__all__ = [
    "CLOCK_FILENAME",
    "CLOCK_MAX_AGE_SECONDS",
    "MT5ServerClockState",
    "load_mt5_server_clock",
    "NTPClockSample",
    "CLOCK_MAX_UNCERTAINTY_SECONDS",
    "estimate_mt5_server_clock",
    "query_ntp_sources",
]

"""Live MT5 trade-server clock bridge.

The Python MT5 API does not expose a direct TimeTradeServer() value. The
production synchronizer estimates the broker timestamp offset from an advancing
terminal tick and independent network UTC observations. It anchors the result
to monotonic time, carries an explicit uncertainty bound, and rejects stale or
inconsistent evidence. A chart-attached MQL5 publisher is not required.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import secrets
import socket
import threading
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from itertools import combinations
from pathlib import Path
from typing import Iterable
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
    synchronization_status: str = "synchronized"
    holdover_age_seconds: float = 0.0

    def as_holdover(self, age_seconds: float) -> "MT5ServerClockState":
        """Return a conservative holdover view without moving the verified anchor."""
        age = float(age_seconds)
        if not math.isfinite(age) or age < 0:
            raise ValueError("holdover age must be finite and non-negative")
        growth = age * CLOCK_HOLDOVER_DRIFT_PPM / 1_000_000.0
        return replace(
            self,
            uncertainty_seconds=self.uncertainty_seconds + growth,
            synchronization_status="holdover",
            holdover_age_seconds=age,
        )

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
# Conservative holdover oscillator bound: 1 ms of added uncertainty per second.
CLOCK_HOLDOVER_DRIFT_PPM = 1_000.0
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


@dataclass(frozen=True)
class UTCClockState:
    """Independent UTC estimate anchored to monotonic time, with uncertainty."""

    observed_utc_epoch: float
    observed_monotonic: float
    uncertainty_seconds: float
    sources: tuple[str, ...]

    def lower_bound(self, now_monotonic: float | None = None) -> float | None:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        age = now - float(self.observed_monotonic)
        if not math.isfinite(age) or age < 0 or age > UTC_CLOCK_MAX_AGE_SECONDS:
            return None
        uncertainty = (
            float(self.uncertainty_seconds)
            + age * CLOCK_HOLDOVER_DRIFT_PPM / 1_000_000.0
        )
        if not math.isfinite(uncertainty) or uncertainty > CLOCK_MAX_UNCERTAINTY_SECONDS:
            return None
        estimate = float(self.observed_utc_epoch) + age
        return estimate - uncertainty


UTC_CLOCK_REFRESH_SECONDS = 30.0
UTC_CLOCK_MAX_AGE_SECONDS = 60.0
_UTC_CLOCK_CACHE: UTCClockState | None = None
_UTC_CLOCK_CACHE_LOCK = threading.Lock()
_UTC_CLOCK_RETRY_AFTER = 0.0


def estimate_utc_clock(
    samples: Iterable[NTPClockSample],
    *,
    observed_monotonic: float | None = None,
) -> UTCClockState | None:
    """Estimate UTC now from a corroborated cluster of independent authorities."""
    now_mono = time.monotonic() if observed_monotonic is None else float(observed_monotonic)
    if not math.isfinite(now_mono):
        return None
    by_authority: dict[str, NTPClockSample] = {}
    for sample in tuple(samples or ()):
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
            quality = float(sample.uncertainty_seconds)
            current_quality = (
                float(current.uncertainty_seconds) if current is not None else float("inf")
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(quality) and quality >= 0 and quality < current_quality:
            by_authority[authority] = sample

    estimates: list[tuple[float, NTPClockSample, float]] = []
    for sample in by_authority.values():
        try:
            uncertainty = float(sample.uncertainty_seconds)
            estimate = (
                float(sample.utc_epoch)
                + now_mono - float(sample.monotonic_epoch)
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            math.isfinite(estimate)
            and math.isfinite(uncertainty)
            and uncertainty >= 0
        ):
            estimates.append((estimate, sample, uncertainty))
    if len(estimates) < NTP_MIN_SOURCES:
        return None

    clusters: list[tuple[tuple[float, NTPClockSample, float], ...]] = []
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

    medians: list[float] = []
    for cluster in clusters:
        ordered = sorted(value for value, _, _ in cluster)
        middle = len(ordered) // 2
        medians.append(
            ordered[middle] if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2.0
        )
    if max(medians) - min(medians) > 0.25:
        return None

    selected = clusters[0]
    ordered = sorted(value for value, _, _ in selected)
    middle = len(ordered) // 2
    median = (
        ordered[middle] if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2.0
    )
    spread = max(abs(value - median) for value in ordered)
    uncertainty = (
        max(item[2] for item in selected) + spread + 0.15
    )
    if uncertainty > CLOCK_MAX_UNCERTAINTY_SECONDS:
        return None
    return UTCClockState(
        observed_utc_epoch=median,
        observed_monotonic=now_mono,
        uncertainty_seconds=uncertainty,
        sources=tuple(sample.server for _, sample, _ in selected),
    )


def clear_independent_utc_clock_cache() -> None:
    """Clear the process UTC cache; intended for deterministic tests."""
    global _UTC_CLOCK_CACHE, _UTC_CLOCK_RETRY_AFTER
    with _UTC_CLOCK_CACHE_LOCK:
        _UTC_CLOCK_CACHE = None
        _UTC_CLOCK_RETRY_AFTER = 0.0


def independent_utc_lower_bound() -> float | None:
    """Return a conservative UTC lower bound without consulting host wall time."""
    global _UTC_CLOCK_CACHE, _UTC_CLOCK_RETRY_AFTER
    now_mono = time.monotonic()
    with _UTC_CLOCK_CACHE_LOCK:
        if _UTC_CLOCK_CACHE is not None:
            age = now_mono - _UTC_CLOCK_CACHE.observed_monotonic
            if age <= UTC_CLOCK_REFRESH_SECONDS:
                return _UTC_CLOCK_CACHE.lower_bound(now_mono)
        if now_mono < _UTC_CLOCK_RETRY_AFTER:
            return (
                _UTC_CLOCK_CACHE.lower_bound(now_mono)
                if _UTC_CLOCK_CACHE is not None else None
            )
        try:
            samples = query_ntp_sources()
            observed_mono = time.monotonic()
            state = estimate_utc_clock(
                samples, observed_monotonic=observed_mono,
            )
        except Exception:
            state = None
            observed_mono = time.monotonic()
        if state is not None:
            _UTC_CLOCK_CACHE = state
            _UTC_CLOCK_RETRY_AFTER = observed_mono + UTC_CLOCK_REFRESH_SECONDS
            return state.lower_bound(observed_mono)
        _UTC_CLOCK_RETRY_AFTER = observed_mono + 5.0
        return (
            _UTC_CLOCK_CACHE.lower_bound(observed_mono)
            if _UTC_CLOCK_CACHE is not None else None
        )


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
        # safe-fallback: malformed source uncertainty cannot corroborate the clock.
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
        # safe-fallback: malformed UTC observations are excluded from consensus.
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
    "UTCClockState",
    "estimate_utc_clock",
    "independent_utc_lower_bound",
    "clear_independent_utc_clock_cache",
    "CLOCK_MAX_UNCERTAINTY_SECONDS",
    "CLOCK_HOLDOVER_DRIFT_PPM",
    "estimate_mt5_server_clock",
    "query_ntp_sources",
]

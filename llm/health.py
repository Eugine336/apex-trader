"""APEX TRADER — Compute health primitives (GPU/Compute Constitution).

Shared, provider-agnostic building blocks so the Model Manager (the Brain's
single reasoner's failover chain) and the Reasoning Orchestrator (the Council)
treat compute the same way:

* :class:`CircuitBreaker` — per-provider live health + a circuit breaker. A
  provider that keeps failing (timeouts / HTTP errors) is tripped OPEN for a
  cooldown (exponential backoff, capped) so it is NOT re-hammered every cycle;
  when the cooldown elapses it is admitted HALF-OPEN for a single probe (the
  next real request) and closes on success or re-opens (longer) on failure.
  Distinguishes ``exists`` (usable config) from ``usable now`` (healthy) —
  Constitution §7.
* :class:`ConcurrencyLimiter` — bounds concurrent calls to a class of providers.
  Used to cap LOCAL (CPU/GPU-bound) model concurrency so a panel does not start
  every local model at once and exhaust the host (the "all Ollama models on →
  CPU outage" failure) — Constitution §3/§11.

Pure standard library; fully thread-safe; fail-safe (never raises on the health
path). Compute is infrastructure — none of this decides a trade; it only decides
which computational resource the Brain/Council reaches.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class CircuitConfig:
    """Circuit-breaker tuning (Constitution §7/§9)."""

    failure_threshold: int = 3          # consecutive failures that trip the circuit OPEN
    cooldown_seconds: float = 30.0      # base OPEN cooldown
    cooldown_max_seconds: float = 300.0  # cap on the exponential backoff

    def sanitized(self) -> "CircuitConfig":
        return CircuitConfig(
            failure_threshold=max(1, int(self.failure_threshold)),
            cooldown_seconds=max(0.0, float(self.cooldown_seconds)),
            cooldown_max_seconds=max(0.0, float(self.cooldown_max_seconds)),
        )


class CircuitBreaker:
    """Live health + circuit breaker for one compute provider. Thread-safe.

    States: CLOSED (admits), OPEN (rejects until cooldown elapses), HALF_OPEN
    (cooldown elapsed — admits exactly the next probe). ``admits`` is what the
    router/council consult to decide whether to *attempt* the provider at all.
    """

    def __init__(self, cfg: Optional[CircuitConfig] = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = (cfg or CircuitConfig()).sanitized()
        self._clock = clock
        self.successes = 0
        self.failures = 0
        self.consecutive_failures = 0
        self.ewma_latency_ms = 0.0
        self.last_error = ""
        self._trips = 0
        self._open_until = 0.0            # monotonic; 0 ⇒ closed
        self._lock = threading.Lock()

    # ── outcome recording ────────────────────────────────────────────────
    def record_success(self, latency_ms: float = 0.0) -> None:
        with self._lock:
            self.successes += 1
            self.consecutive_failures = 0
            self._trips = 0
            self._open_until = 0.0
            self.last_error = ""
            self._ewma(latency_ms)

    def record_failure(self, latency_ms: float = 0.0, error: str = "") -> None:
        with self._lock:
            self.failures += 1
            self.consecutive_failures += 1
            if error:
                self.last_error = str(error)[:160]
            self._ewma(latency_ms)
            if self.consecutive_failures >= self.cfg.failure_threshold:
                self._trips += 1
                backoff = self.cfg.cooldown_seconds * (2 ** (self._trips - 1))
                self._open_until = self._clock() + min(backoff, self.cfg.cooldown_max_seconds)

    def _ewma(self, latency_ms: float) -> None:
        if latency_ms and latency_ms > 0:
            a = 0.3
            self.ewma_latency_ms = (latency_ms if self.ewma_latency_ms <= 0.0
                                    else (1 - a) * self.ewma_latency_ms + a * latency_ms)

    # ── state queries ─────────────────────────────────────────────────────
    def admits(self, now: Optional[float] = None) -> bool:
        """True when the provider may be attempted (CLOSED or HALF_OPEN)."""
        with self._lock:
            if self._open_until <= 0.0:
                return True
            t = self._clock() if now is None else float(now)
            return t >= self._open_until

    def is_open(self, now: Optional[float] = None) -> bool:
        return not self.admits(now)

    def state(self, now: Optional[float] = None) -> str:
        with self._lock:
            if self._open_until <= 0.0:
                return "closed"
            t = self._clock() if now is None else float(now)
            return "half_open" if t >= self._open_until else "open"

    def seconds_until_retry(self, now: Optional[float] = None) -> float:
        with self._lock:
            if self._open_until <= 0.0:
                return 0.0
            t = self._clock() if now is None else float(now)
            return max(0.0, self._open_until - t)

    @property
    def attempts(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float:
        # Optimistic prior — an untried provider is tried before a known-bad one.
        return 1.0 if self.attempts == 0 else self.successes / self.attempts

    def to_dict(self, now: Optional[float] = None) -> dict:
        return {
            "state": self.state(now),
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "success_rate": round(self.success_rate, 4),
            "ewma_latency_ms": round(self.ewma_latency_ms, 1),
            "cooldown_remaining_s": round(self.seconds_until_retry(now), 1),
            "last_error": self.last_error,
        }


class ConcurrencyLimiter:
    """Bounds concurrent calls to a class of providers (e.g. local CPU/GPU-bound
    models). ``limit <= 0`` ⇒ unbounded (a transparent no-op). Fail-safe."""

    def __init__(self, limit: int) -> None:
        try:
            self._limit = int(limit)
        except (TypeError, ValueError):
            self._limit = 0
        self._sem: Optional[threading.Semaphore] = (
            threading.Semaphore(self._limit) if self._limit > 0 else None
        )

    @property
    def limit(self) -> int:
        return self._limit

    @contextmanager
    def slot(self, timeout: Optional[float] = None):
        """Context manager yielding True if a slot was acquired, else False.

        Unbounded ⇒ always True. Bounded ⇒ blocks up to ``timeout`` seconds
        (None = block indefinitely); yields False if it could not acquire in
        time so the caller can skip rather than pile onto an exhausted host."""
        if self._sem is None:
            yield True
            return
        acquired = self._sem.acquire(timeout=timeout) if timeout is not None else self._sem.acquire()
        try:
            yield bool(acquired)
        finally:
            if acquired:
                self._sem.release()


__all__ = ["CircuitConfig", "CircuitBreaker", "ConcurrencyLimiter"]

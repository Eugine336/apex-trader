"""
APEX TRADER — Circuit Breaker
Protects the system from hammering broken APIs.
CLOSED → normal operation.
OPEN → blocked, cooldown running.
HALF_OPEN → testing recovery with limited attempts.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from typing import Optional


class CircuitState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class CircuitStatus:
    state: CircuitState
    failure_count: int
    last_failure_time: Optional[datetime]
    cooldown_remaining_seconds: float
    last_success_time: Optional[datetime]


class CircuitBreaker:
    """Protects operations from repeated failures using the circuit breaker pattern."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        cooldown_seconds: float = 300,
        half_open_max_failures: int = 2,
    ):
        self.name = name
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._half_open_max_failures = half_open_max_failures
        self._last_failure_time: Optional[datetime] = None
        self._last_success_time: Optional[datetime] = None
        self._half_open_failures = 0
        # Trading loop and health-check threads can hit this concurrently;
        # serialise all state reads/mutations so counters never tear.
        self._lock = Lock()

    def can_execute(self) -> bool:
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if self._cooldown_expired():
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_failures = 0
                    return True
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._half_open_failures = 0
            self._state = CircuitState.CLOSED
            self._last_success_time = datetime.now(timezone.utc)

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = datetime.now(timezone.utc)
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_failures += 1
                if self._half_open_failures >= self._half_open_max_failures:
                    self._state = CircuitState.OPEN
            elif self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN

    def get_status(self) -> CircuitStatus:
        with self._lock:
            cooldown = 0.0
            if self._state == CircuitState.OPEN and self._last_failure_time:
                elapsed = (
                    datetime.now(timezone.utc) - self._last_failure_time
                ).total_seconds()
                cooldown = max(0.0, self._cooldown_seconds - elapsed)
            return CircuitStatus(
                state=self._state,
                failure_count=self._failure_count,
                last_failure_time=self._last_failure_time,
                cooldown_remaining_seconds=cooldown,
                last_success_time=self._last_success_time,
            )

    def _cooldown_expired(self) -> bool:
        if not self._last_failure_time:
            return True
        elapsed = (
            datetime.now(timezone.utc) - self._last_failure_time
        ).total_seconds()
        return elapsed >= self._cooldown_seconds

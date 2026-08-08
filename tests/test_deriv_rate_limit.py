"""Rate-limit compliance for Deriv ``ticks_history`` requests.

C4: the history rate-limit wait used to happen *inside* the global send lock,
serialising every Deriv call behind it. The wait now happens outside any lock
via ``_reserve_history_slot``, which atomically reserves spaced send slots so
the per-second rate limit is preserved even when many threads request history
concurrently. These tests exercise the reservation logic directly (no network).
"""

import threading
import time

import platforms.deriv.deriv_connector as dc
from platforms.deriv.deriv_connector import DerivConnector


def _make_bare_connector() -> DerivConnector:
    """A connector with only the rate-limit state initialised (no WS/loop)."""
    conn = DerivConnector.__new__(DerivConnector)
    conn._history_lock = threading.Lock()
    conn._next_history_at = 0.0
    conn._last_history_request = 0.0
    return conn


def test_reserve_history_slot_spaces_sequential_requests():
    conn = _make_bare_connector()
    interval = dc._HISTORY_MIN_INTERVAL
    start = time.monotonic()
    # Reserve far more slots than fit in real time, so every call after the
    # first sees now < next and reserves the next interval-spaced slot.
    n = 20
    for _ in range(n):
        conn._reserve_history_slot()
    advanced = conn._next_history_at - start
    # n distinct slots, each spaced by `interval`, were reserved.
    assert advanced >= (n - 1) * interval
    assert advanced <= (n + 1) * interval


def test_reserve_history_slot_is_concurrency_safe():
    conn = _make_bare_connector()
    interval = dc._HISTORY_MIN_INTERVAL
    threads_count = 8
    per_thread = 10
    total = threads_count * per_thread
    start = time.monotonic()

    def worker():
        for _ in range(per_thread):
            wait = conn._reserve_history_slot()
            assert wait >= 0.0  # never negative

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    advanced = conn._next_history_at - start
    # Every one of the `total` concurrent reservations got a distinct slot
    # spaced by `interval` — no two threads were granted the same slot (which
    # would let them send too close together and breach the rate limit).
    assert advanced >= (total - 1) * interval
    assert advanced <= (total + 1) * interval


def test_reserve_does_not_sleep_inside_the_lock():
    """The reservation must return immediately; the wait is slept by the caller
    (in _sync_send) OUTSIDE the lock. So issuing many reservations is fast even
    though they schedule sends seconds apart."""
    conn = _make_bare_connector()
    n = 50
    t0 = time.monotonic()
    for _ in range(n):
        conn._reserve_history_slot()
    elapsed = time.monotonic() - t0
    # n reservations schedule n * interval seconds of spacing, but reserving
    # them must take a tiny fraction of that — proving no sleep under the lock.
    assert elapsed < dc._HISTORY_MIN_INTERVAL

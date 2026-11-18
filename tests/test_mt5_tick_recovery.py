"""WP-T4: bounded, verified, idempotent tick-history recovery after an interruption."""

from datetime import datetime, timedelta, timezone

import importlib.util
import pathlib

# Reuse the poller fakes from the bootstrap decomposition tests (that module loads the
# poller under test with light stubs, so import it by path).
_spec = importlib.util.spec_from_file_location(
    "_bootstrap_decomposition_helpers", pathlib.Path(__file__).with_name("test_bootstrap_decomposition.py"),
)
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
_history_quote, _history_row = _helpers._history_quote, _helpers._history_row
_HistoryFrame, _HistoryPM, _Router = _helpers._HistoryFrame, _helpers._HistoryPM, _helpers._Router
tick_sources = _helpers.tick_sources

T0 = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)


class _ChunkPM(_HistoryPM):
    """History provider that answers every window; ``mode`` controls what it returns."""

    def __init__(self, latest, mode="ticks"):
        super().__init__(latest, [])
        self.mode = mode

    def get_tick_history(self, symbol, start, end):
        self.history_calls.append((symbol, start, end))
        if self.mode == "unverified":
            return _HistoryFrame([_history_row(start + timedelta(seconds=1), 1.1, 1.1002)], verified=False)
        if self.mode == "empty":
            return _HistoryFrame([])
        if self.mode == "empty_first" and len(self.history_calls) == 1:
            return _HistoryFrame([])
        stamp = start + timedelta(seconds=1)
        return _HistoryFrame([_history_row(stamp, 1.1000, 1.1002)])


def _poller(latest_offset_seconds, mode="ticks", cursor=T0):
    latest = T0 + timedelta(seconds=latest_offset_seconds)
    pm = _ChunkPM(_history_quote(latest, 1.1003, 1.1005), mode)
    router = _Router()
    poller = tick_sources.MT5TickPoller(pm, router, ["EURUSD"], poll_interval=0.0)
    if cursor is not None:
        poller._history_cursor["EURUSD"] = cursor
    return poller, pm, router


def test_a_twenty_minute_interruption_is_recovered_in_verified_chunks_without_a_gap():
    poller, pm, router = _poller(1200)
    poller._poll_symbol("EURUSD")

    assert len(pm.history_calls) == 4                              # 1200 s / 300 s chunks
    assert poller._history_gap_count.get("EURUSD", 0) == 0
    stamps = [t.timestamp for t in router.ticks]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)   # ordered, no duplicates
    assert all(t.source_timestamp_verified for t in router.ticks)
    assert poller._history_cursor["EURUSD"] >= T0 + timedelta(seconds=900)


def test_consecutive_chunks_overlap_by_one_millisecond_only_and_cover_the_whole_span():
    poller, pm, _ = _poller(900)
    poller._poll_symbol("EURUSD")
    calls = pm.history_calls
    assert calls[0][1] == T0 - timedelta(milliseconds=1)
    assert calls[-1][2] == T0 + timedelta(seconds=900)
    for (_, _, previous_end), (_, next_start, _) in zip(calls, calls[1:]):
        assert next_start <= previous_end                          # no uncovered hole between chunks


def test_an_empty_interior_chunk_is_skipped_but_nothing_is_invented():
    poller, pm, router = _poller(900, mode="empty_first")
    poller._poll_symbol("EURUSD")
    assert len(pm.history_calls) == 3
    assert poller._history_gap_count.get("EURUSD", 0) == 0
    assert all(t.timestamp > T0 for t in router.ticks)             # the empty chunk produced no ticks


def test_beyond_the_recovery_horizon_only_the_newest_hour_is_recovered_and_the_rest_is_a_recorded_gap():
    poller, pm, _ = _poller(2 * 3600)
    poller._poll_symbol("EURUSD")
    assert poller._history_gap_count["EURUSD"] == 1
    assert len(pm.history_calls) == 12                             # 3600 s / 300 s
    assert pm.history_calls[0][1] == T0 + timedelta(seconds=3600)  # older hour never requested


def test_an_unverified_chunk_in_a_long_recovery_records_a_gap_and_reanchors_without_routing_history():
    poller, pm, router = _poller(1200, mode="unverified")
    poller._poll_symbol("EURUSD")
    assert len(pm.history_calls) == 1                              # recovery stops at the first unverifiable chunk
    assert poller._history_gap_count["EURUSD"] == 1
    assert poller._history_cursor["EURUSD"] == T0 + timedelta(seconds=1200)
    assert len(router.ticks) == 1                                  # only the current latest quote, never history


def test_an_unverified_short_window_is_retried_not_recorded_as_a_gap():
    poller, pm, router = _poller(120, mode="unverified")
    poller._poll_symbol("EURUSD")
    assert poller._history_gap_count.get("EURUSD", 0) == 0
    assert poller._history_cursor["EURUSD"] == T0                  # cursor unchanged, retried next poll


def test_recovery_is_idempotent_when_the_same_window_is_polled_twice():
    poller, pm, router = _poller(600)
    poller._poll_symbol("EURUSD")
    first = [t.timestamp for t in router.ticks]
    poller._poll_symbol("EURUSD")
    second = [t.timestamp for t in router.ticks]
    assert len(second) == len(set(second))                         # no tick routed twice
    assert second[: len(first)] == first

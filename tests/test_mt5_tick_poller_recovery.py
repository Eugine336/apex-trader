"""Regression tests for MT5 tick-history recovery and checkpointing."""

from datetime import datetime, timedelta, timezone
from collections import deque
import json

import pandas as pd

from tick.tick_sources import MT5TickPoller


class _Router:
    def __init__(self):
        self.ticks = []

    def on_tick(self, tick):
        self.ticks.append(tick)


class _PlatformManager:
    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.calls = []

    def get_tick_history(self, symbol, start, end):
        self.calls.append((start, end))
        return self.fetcher(symbol, start, end)


class _CheckpointStore:
    def __init__(self):
        self.rows = []
        self.fail_writes = False

    def query_events(self, **kwargs):
        return list(reversed(self.rows[-kwargs.get("limit", 10000):]))

    def emit(self, **kwargs):
        assert kwargs["durable"] is True
        if self.fail_writes:
            raise RuntimeError("injected durable write failure")
        self.rows.append({
            "symbol": kwargs["symbol"],
            "payload_json": json.dumps(kwargs["payload"]),
        })
        return "checkpoint-id"


def _verified_frame(start, end):
    stamp = start + timedelta(seconds=1)
    epoch_ms = int(stamp.timestamp() * 1000)
    frame = pd.DataFrame([{
        "time": int(stamp.timestamp()),
        "time_msc": epoch_ms,
        "time_source_epoch": int(stamp.timestamp()),
        "time_msc_source_epoch": epoch_ms,
        "time_server_epoch": int(stamp.timestamp()),
        "time_msc_server_epoch": epoch_ms,
        "time_msc_datetime": stamp,
        "bid": 2000.0,
        "ask": 2000.1,
        "flags": 0,
    }])
    frame.attrs.update({
        "source_timestamp_verified": True,
        "source_timezone": "UTC",
        "utc_normalization": "utc_request_bounds_verified",
    })
    return frame


def test_long_history_gap_is_recovered_in_verified_300_second_batches():
    start = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=601)
    calls = []

    def fetcher(symbol, batch_start, batch_end):
        calls.append((batch_start, batch_end))
        return _verified_frame(batch_start, batch_end)

    pm = _PlatformManager(fetcher)
    router = _Router()
    poller = MT5TickPoller(pm, router, ["XAUUSD"])
    poller._history_cursor["XAUUSD"] = start + timedelta(milliseconds=1)

    latest = type("Quote", (), {
        "time": end,
        "raw_fields": {"broker_server_identity": "JustMarkets-Demo"},
    })()

    assert poller._route_history_quotes("XAUUSD", latest) is True
    assert len(calls) == 3
    assert [round((right - left).total_seconds(), 3) for left, right in calls] == [
        300.0, 300.0, 1.002,
    ]
    assert len(router.ticks) == 3
    assert poller._history_cursor["XAUUSD"] == calls[-1][0] + timedelta(seconds=1)


def test_failed_batch_does_not_advance_cursor_past_unverified_gap():
    start = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=601)
    calls = []

    def fetcher(symbol, batch_start, batch_end):
        calls.append((batch_start, batch_end))
        if len(calls) == 2:
            frame = pd.DataFrame([{
                "time": int((batch_start + timedelta(seconds=1)).timestamp()),
                "time_msc": int((batch_start + timedelta(seconds=1)).timestamp() * 1000),
                "time_msc_datetime": batch_start + timedelta(seconds=1),
                "bid": 2000.0, "ask": 2000.1, "flags": 0,
            }])
            frame.attrs.update({
                "source_timestamp_verified": False,
                "source_timezone": "unknown",
                "utc_normalization": "unverified_source_epoch",
            })
            return frame
        return _verified_frame(batch_start, batch_end)

    pm = _PlatformManager(fetcher)
    router = _Router()
    poller = MT5TickPoller(pm, router, ["XAUUSD"])
    poller._history_cursor["XAUUSD"] = start + timedelta(milliseconds=1)
    latest = type("Quote", (), {
        "time": end,
        "raw_fields": {"broker_server_identity": "JustMarkets-Demo"},
    })()

    assert poller._route_history_quotes("XAUUSD", latest) is False
    assert len(calls) == 2
    assert len(router.ticks) == 1
    assert poller._history_cursor["XAUUSD"] == calls[0][0] + timedelta(seconds=1)


def test_replay_checkpoint_is_durable_and_broker_identity_scoped():
    start = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=5)
    store = _CheckpointStore()
    pm = _PlatformManager(lambda _symbol, left, right: _verified_frame(left, right))
    poller = MT5TickPoller(pm, _Router(), ["XAUUSD"], checkpoint_store=store)
    poller._history_seen_order["XAUUSD"] = deque([
        (int(end.timestamp() * 1000), 2000.0, 2000.1, 0),
    ])
    assert poller._persist_history_checkpoint("XAUUSD", end, "JustMarkets-Demo") is True
    assert len(store.rows) == 1
    payload = json.loads(store.rows[0]["payload_json"])
    assert payload["broker_server"] == "JustMarkets-Demo"
    assert payload["cursor_utc"] == end.isoformat()

    restored = MT5TickPoller(pm, _Router(), ["XAUUSD"], checkpoint_store=store)
    restored._apply_history_checkpoint("XAUUSD", "JustMarkets-Demo")
    assert restored._history_cursor["XAUUSD"] == end
    assert len(restored._history_seen["XAUUSD"]) == 1


def test_checkpoint_from_different_broker_server_is_rejected():
    store = _CheckpointStore()
    poller = MT5TickPoller(_PlatformManager(lambda *_args: pd.DataFrame()), _Router(),
                           ["XAUUSD"], checkpoint_store=store)
    poller._restored_history_checkpoints["XAUUSD"] = {
        "schema": "apex.mt5_tick_checkpoint.v1",
        "broker_server": "Other-Demo",
        "cursor_utc": "2026-11-11T10:00:00+00:00",
        "recent_fingerprints": [],
    }

    poller._apply_history_checkpoint("XAUUSD", "JustMarkets-Demo")

    assert "XAUUSD" not in poller._history_cursor
    assert poller._history_gap_count["XAUUSD"] == 1



def test_checkpoint_write_failure_is_reported_and_not_persisted():
    store = _CheckpointStore()
    store.fail_writes = True
    poller = MT5TickPoller(_PlatformManager(lambda *_args: pd.DataFrame()), _Router(),
                           ["XAUUSD"], checkpoint_store=store)
    cursor = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)

    assert poller._persist_history_checkpoint("XAUUSD", cursor, "JustMarkets-Demo") is False
    assert store.rows == []
    assert "XAUUSD" not in poller._last_checkpoint_cursor


def test_checkpoint_write_is_rate_limited_without_losing_replay_overlap():
    store = _CheckpointStore()
    poller = MT5TickPoller(_PlatformManager(lambda *_args: pd.DataFrame()), _Router(),
                           ["XAUUSD"], checkpoint_store=store)
    first = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)

    assert poller._persist_history_checkpoint("XAUUSD", first, "JustMarkets-Demo") is True
    assert poller._persist_history_checkpoint(
        "XAUUSD", first + timedelta(seconds=2), "JustMarkets-Demo"
    ) is True
    assert len(store.rows) == 1
    assert poller._persist_history_checkpoint(
        "XAUUSD", first + timedelta(seconds=6), "JustMarkets-Demo"
    ) is True
    assert len(store.rows) == 2


def test_unrecoverable_history_gap_is_durably_recorded_once():
    store = _CheckpointStore()
    poller = MT5TickPoller(_PlatformManager(lambda *_args: pd.DataFrame()), _Router(),
                           ["XAUUSD"], checkpoint_store=store)
    start = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=30)

    poller._persist_history_gap("XAUUSD", start, end, "JustMarkets-Demo", "history_unverified")
    poller._persist_history_gap("XAUUSD", start, end, "JustMarkets-Demo", "history_unverified")

    assert len(store.rows) == 1
    payload = json.loads(store.rows[0]["payload_json"])
    assert payload["schema"] == "apex.mt5_tick_gap.v1"
    assert payload["reason"] == "history_unverified"



def test_event_store_checkpoint_survives_real_store_restart(tmp_path):
    from persistence.event_store import EventStore

    db_path = tmp_path / "mt5-recovery-test.sqlite"
    store = EventStore(db_path=str(db_path))
    cursor = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
    first = MT5TickPoller(_PlatformManager(lambda *_args: pd.DataFrame()), _Router(),
                          ["XAUUSD"], checkpoint_store=store)
    assert first._persist_history_checkpoint("XAUUSD", cursor, "JustMarkets-Demo") is True
    store.close()

    reopened_store = EventStore(db_path=str(db_path))
    recovered = MT5TickPoller(
        _PlatformManager(lambda *_args: pd.DataFrame()), _Router(),
        ["XAUUSD"], checkpoint_store=reopened_store,
    )
    recovered._apply_history_checkpoint("XAUUSD", "JustMarkets-Demo")

    assert recovered._history_cursor["XAUUSD"] == cursor
    reopened_store.close()



def test_broker_server_change_discards_prior_in_memory_cursor_and_dedup_state():
    end = datetime(2026, 11, 11, 10, 5, tzinfo=timezone.utc)
    pm = _PlatformManager(lambda _symbol, left, right: _verified_frame(left, right))
    poller = MT5TickPoller(pm, _Router(), ["XAUUSD"])
    poller._history_broker_identity["XAUUSD"] = "Old-Demo"
    poller._history_cursor["XAUUSD"] = end - timedelta(seconds=1)
    poller._history_seen["XAUUSD"] = {(1, 2000.0, 2000.1, 0)}
    poller._history_seen_order["XAUUSD"] = deque([(1, 2000.0, 2000.1, 0)])
    latest = type("Quote", (), {
        "time": end,
        "raw_fields": {"broker_server_identity": "JustMarkets-Demo"},
    })()

    assert poller._route_history_quotes("XAUUSD", latest) is True
    assert pm.calls[0][0] == end - timedelta(seconds=300)
    assert poller._history_broker_identity["XAUUSD"] == "JustMarkets-Demo"
    assert poller._history_gap_count["XAUUSD"] == 1



def test_future_replay_cursor_is_cleared_and_next_poll_reanchors_boundedly():
    end = datetime(2026, 11, 11, 10, 5, tzinfo=timezone.utc)
    pm = _PlatformManager(lambda _symbol, left, right: _verified_frame(left, right))
    poller = MT5TickPoller(pm, _Router(), ["XAUUSD"])
    poller._history_broker_identity["XAUUSD"] = "JustMarkets-Demo"
    poller._history_cursor["XAUUSD"] = end + timedelta(seconds=60)
    latest = type("Quote", (), {
        "time": end,
        "raw_fields": {"broker_server_identity": "JustMarkets-Demo"},
    })()

    assert poller._route_history_quotes("XAUUSD", latest) is False
    assert "XAUUSD" not in poller._history_cursor
    assert poller._history_gap_count["XAUUSD"] == 1

    assert poller._route_history_quotes("XAUUSD", latest) is True
    assert pm.calls[0][0] == end - timedelta(seconds=300)

def test_latest_quote_and_utc_history_share_normalized_dedup_identity():
    from types import SimpleNamespace

    start = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
    event_time = start + timedelta(seconds=1)
    event_epoch = event_time.timestamp()
    pm = _PlatformManager(lambda _symbol, left, right: _verified_frame(left, right))
    router = _Router()
    poller = MT5TickPoller(pm, router, ["XAUUSD"])
    # Pin the replay start so the fixture's event is the exact history event
    # represented by the latest quote below.
    poller._history_cursor["XAUUSD"] = start + timedelta(milliseconds=1)

    history_latest = SimpleNamespace(
        time=event_time,
        raw_fields={"broker_server_identity": "JustMarkets-Demo"},
    )
    assert poller._route_history_quotes("XAUUSD", history_latest) is True
    assert len(router.ticks) == 1

    # The latest-quote API exposes a broker-server epoch (+2h here), while
    # the history API exposes the same event in UTC. The normalized UTC epoch
    # must make these two provider representations deduplicate as one event.
    live_quote = SimpleNamespace(
        time=event_time,
        bid=2000.0,
        ask=2000.1,
        source_timestamp_verified=True,
        quote_kind="bid_ask",
        spread_unit="ticks",
        raw_fields={
            "time_msc": int((event_epoch + 7200) * 1000),
            "timestamp_utc_epoch": event_epoch,
            "flags": 0,
        },
    )
    assert poller._route_latest_quote("XAUUSD", live_quote) is False
    assert len(router.ticks) == 1


def test_durable_gap_write_failure_remains_retryable():
    store = _CheckpointStore()
    store.fail_writes = True
    poller = MT5TickPoller(
        _PlatformManager(lambda *_args: pd.DataFrame()),
        _Router(),
        ["XAUUSD"],
        checkpoint_store=store,
    )
    start = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=30)

    poller._persist_history_gap(
        "XAUUSD", start, end, "JustMarkets-Demo", "history_unverified",
    )
    assert store.rows == []
    assert not poller._history_gap_reported

    store.fail_writes = False
    poller._persist_history_gap(
        "XAUUSD", start, end, "JustMarkets-Demo", "history_unverified",
    )
    poller._persist_history_gap(
        "XAUUSD", start, end, "JustMarkets-Demo", "history_unverified",
    )

    assert len(store.rows) == 1
    assert poller._history_gap_reported

def test_server_identity_change_allows_new_server_checkpoint_to_be_written():
    end = datetime(2026, 11, 11, 10, 5, tzinfo=timezone.utc)
    store = _CheckpointStore()
    pm = _PlatformManager(lambda _symbol, left, right: _verified_frame(left, right))
    poller = MT5TickPoller(
        pm, _Router(), ["XAUUSD"], checkpoint_store=store,
    )
    poller._history_broker_identity["XAUUSD"] = "Old-Demo"
    poller._history_cursor["XAUUSD"] = end - timedelta(seconds=1)
    poller._last_checkpoint_cursor["XAUUSD"] = end + timedelta(seconds=60)
    latest = type("Quote", (), {
        "time": end,
        "raw_fields": {"broker_server_identity": "JustMarkets-Demo"},
    })()

    assert poller._route_history_quotes("XAUUSD", latest) is True
    assert len(store.rows) == 1
    payload = json.loads(store.rows[0]["payload_json"])
    assert payload["broker_server"] == "JustMarkets-Demo"
    assert payload["cursor_utc"] == (pm.calls[0][0] + timedelta(seconds=1)).isoformat()

def test_history_processing_exception_does_not_block_live_quote_delivery():
    from types import SimpleNamespace

    stamp = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)

    class _MalformedHistory:
        attrs = {
            "source_timestamp_verified": True,
            "source_timezone": "UTC",
            "utc_normalization": "utc_request_bounds_verified",
        }
        empty = False

        def iterrows(self):
            raise RuntimeError("injected malformed history iterator")

    class _PM:
        def get_price(self, _symbol):
            return SimpleNamespace(
                symbol="XAUUSD",
                bid=2000.0,
                ask=2000.1,
                time=stamp,
                source_timestamp_verified=True,
                quote_kind="bid_ask",
                spread_unit="ticks",
                raw_fields={
                    "time": int(stamp.timestamp()) + 7200,
                    "time_msc": int(stamp.timestamp() * 1000) + 7_200_000,
                    "timestamp_utc_epoch": stamp.timestamp(),
                    "broker_server_identity": "JustMarkets-Demo",
                    "flags": 0,
                },
            )

        def get_tick_history(self, _symbol, _start, _end):
            return _MalformedHistory()

    router = _Router()
    poller = MT5TickPoller(_PM(), router, ["XAUUSD"])

    poller._poll_symbol("XAUUSD")

    assert len(router.ticks) == 1
    assert router.ticks[0].source_timestamp_verified is True
    assert router.ticks[0].timestamp == stamp

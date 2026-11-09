from __future__ import annotations

import os

from platforms.mt5.clock import CLOCK_FILENAME, load_mt5_server_clock


def _write_clock(tmp_path, *, server="JustMarkets-Demo", offset=7200,
                 server_epoch=1_800_007_200, gmt_epoch=1_800_000_000):
    path = tmp_path / "MQL5" / "Files" / CLOCK_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text(
        "schema_version=1\n"
        f"server={server}\n"
        f"offset_seconds={offset}\n"
        f"observed_server_epoch={server_epoch}\n"
        f"observed_gmt_epoch={gmt_epoch}\n",
        encoding="utf-8",
    )
    os.utime(path, (1_800_000_000, 1_800_000_000))
    return path


def test_load_fresh_server_clock_and_normalize_server_epoch(tmp_path):
    _write_clock(tmp_path)
    state = load_mt5_server_clock(
        tmp_path, "JustMarkets-Demo", now_epoch=1_800_000_005,
    )

    assert state is not None
    assert state.offset_seconds == 7200
    assert state.to_utc_epoch(1_800_007_210) == 1_800_000_010
    assert state.server_now_epoch(1_800_000_005) == 1_800_007_205
    assert state.age_seconds(1_800_007_200, 1_800_000_005) == 5


def test_clock_reader_rejects_stale_publisher_file(tmp_path):
    _write_clock(tmp_path)
    state = load_mt5_server_clock(
        tmp_path, "JustMarkets-Demo", now_epoch=1_800_000_100,
        max_age_seconds=20,
    )
    assert state is None


def test_clock_reader_rejects_different_broker_server(tmp_path):
    _write_clock(tmp_path)
    assert load_mt5_server_clock(
        tmp_path, "JustMarkets-Live", now_epoch=1_800_000_005,
    ) is None


def test_clock_reader_rejects_inconsistent_offset_payload(tmp_path):
    _write_clock(tmp_path, offset=3600)
    assert load_mt5_server_clock(
        tmp_path, "JustMarkets-Demo", now_epoch=1_800_000_005,
    ) is None


def test_clock_reader_rejects_missing_or_implausible_clock(tmp_path):
    assert load_mt5_server_clock(
        tmp_path, "JustMarkets-Demo", now_epoch=1_800_000_005,
    ) is None
    _write_clock(tmp_path, offset=99 * 3600)
    assert load_mt5_server_clock(
        tmp_path, "JustMarkets-Demo", now_epoch=1_800_000_005,
    ) is None

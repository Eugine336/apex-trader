from pathlib import Path


def test_mt5_timestamp_diagnostic_is_read_only():
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "diagnose_mt5_timestamp_semantics.py"
    ).read_text(encoding="utf-8")
    for required in (
        "symbol_info_tick",
        "copy_ticks_range",
        "time_msc",
        "query_ntp_sources",
        "time.monotonic",
        "timezone.utc",
        "orders_submitted",
    ):
        assert required in source
    for forbidden in (
        "order_send(", "order_send_async(", "order_check(",
        "positions_get(", "history_orders_get(",
        "os.environ", "os.putenv(", "os.environ[",
    ):
        assert forbidden not in source.lower()

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
import sys


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "diagnose_mt5_timestamp_semantics.py"
)


def _load_diagnostic():
    spec = spec_from_file_location("mt5_timestamp_diagnostic", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_diagnostic_bootstraps_repository_root_for_direct_execution(monkeypatch):
    module = _load_diagnostic()
    repository_root = str(SCRIPT.resolve().parents[1])
    isolated_path = [entry for entry in sys.path if entry != repository_root]
    monkeypatch.setattr(sys, "path", isolated_path)

    resolved = module._ensure_repository_root_on_path()

    assert str(resolved) == repository_root
    assert sys.path[0] == repository_root


def test_symbol_snapshot_exposes_feed_state_without_sensitive_fields():
    module = _load_diagnostic()
    info = SimpleNamespace(
        name="XAUUSD",
        visible=True,
        select=True,
        custom=False,
        trade_mode=4,
        bid=2000.1,
        ask=2000.3,
        last=2000.2,
        volume=12,
        time=1_794_392_463,
        time_msc=1_794_392_463_123,
        spread=2,
        digits=2,
        point=0.01,
        path="Broker\\Metals\\XAUUSD",
        description="Gold",
    )

    snapshot = module._symbol_snapshot(info)

    assert snapshot["name"] == "XAUUSD"
    assert snapshot["visible"] is True
    assert snapshot["select"] is True
    assert snapshot["bid"] == 2000.1
    assert snapshot["time_msc"] == 1_794_392_463_123
    assert "path" not in snapshot
    assert "description" not in snapshot


def test_matching_symbol_names_are_filtered_and_bounded():
    module = _load_diagnostic()
    symbols = [
        SimpleNamespace(name="XAUUSD"),
        SimpleNamespace(name="XAUUSD.a"),
        SimpleNamespace(name="EURUSD"),
        SimpleNamespace(name="xauusd-pro"),
    ]

    assert module._matching_symbol_names(symbols, "XAUUSD") == [
        "XAUUSD",
        "XAUUSD.a",
        "xauusd-pro",
    ]


def test_mt5_timestamp_diagnostic_is_read_only():
    source = SCRIPT.read_text(encoding="utf-8")
    for required in (
        "symbol_info",
        "symbol_info_tick",
        "copy_ticks_range",
        "time_msc",
        "query_ntp_sources",
        "time.monotonic",
        "timezone.utc",
        "orders_submitted",
        "selection_changed",
    ):
        assert required in source
    for forbidden in (
        "symbol_select(",
        "order_send(", "order_send_async(", "order_check(",
        "positions_get(", "history_orders_get(",
        "os.environ", "os.putenv(", "os.environ[",
    ):
        assert forbidden not in source.lower()

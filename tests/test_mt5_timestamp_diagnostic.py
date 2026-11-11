from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
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


def test_mt5_timestamp_diagnostic_is_read_only():
    source = SCRIPT.read_text(encoding="utf-8")
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

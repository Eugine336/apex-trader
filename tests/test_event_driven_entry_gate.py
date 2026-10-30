from types import SimpleNamespace

from event_driven_bootstrap import EventDrivenSystem


def test_scanner_blind_or_unknown_health_blocks_session_entry_gate():
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._scanner_blind_warned = True
    system._ctx = None
    assert system._check_session_active("XAUUSD") is False


def test_session_engine_failure_blocks_entry():
    class BrokenSessionEngine:
        def get_status(self):
            raise RuntimeError("session state unavailable")

    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._scanner_blind_warned = False
    system._ctx = SimpleNamespace(session_engine=BrokenSessionEngine())
    assert system._check_session_active("XAUUSD") is False

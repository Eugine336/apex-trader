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

def test_session_entry_gate_uses_verified_utc_for_session_and_weekend_buffer():
    observed = {}

    class SessionEngine:
        def get_status(self, utc_now):
            observed["status_time"] = utc_now
            return SimpleNamespace(is_tradeable=True)

        def minutes_to_fx_close(self, utc_now):
            observed["close_time"] = utc_now
            return 60

    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._scanner_blind_warned = False
    system._ctx = SimpleNamespace(session_engine=SessionEngine())
    system._pm = SimpleNamespace(
        get_utc_time_lower_bound=lambda _symbol: 1_800_000_000.0,
    )
    system._config = SimpleNamespace(
        risk=SimpleNamespace(weekend_close_buffer_minutes=15),
    )

    assert system._check_session_active("EURUSD") is True
    assert observed["status_time"].tzinfo == timezone.utc
    assert observed["close_time"] == observed["status_time"]


def test_session_entry_gate_blocks_when_verified_utc_is_unknown():
    class SessionEngine:
        called = False

        def get_status(self, _utc_now):
            self.called = True
            return SimpleNamespace(is_tradeable=True)

    session = SessionEngine()
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._scanner_blind_warned = False
    system._ctx = SimpleNamespace(session_engine=session)
    system._pm = SimpleNamespace(get_utc_time_lower_bound=lambda _symbol: None)

    assert system._check_session_active("XAUUSD") is False
    assert session.called is False

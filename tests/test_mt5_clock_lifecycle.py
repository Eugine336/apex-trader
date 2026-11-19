"""WP-T2 lifecycle and M1 cache checks exercise the production connector with a fake IPC."""
from dataclasses import replace
from types import SimpleNamespace
import sys

import pytest

from platforms.mt5 import mt5_connector as adapter
from platforms.mt5.clock import CLOCK_MAX_AGE_SECONDS, NTPClockSample
from platforms.mt5.process_witness import terminal_process_for


class Terminal:
    def __init__(self):
        self.mono = 100.0
        self.offset = 7200.0
        self.process = 1
        self.connected = True
        self.login = 17
        self.server = "Broker-Demo"
        self.data = r"C:\MT5\demo"
        self.quote = None
        self.proofs = 0
        self.witness_shift = 0.0

    def account_info(self):
        return SimpleNamespace(server=self.server, login=self.login)

    def terminal_info(self):
        return SimpleNamespace(connected=self.connected, path=r"C:\MT5", data_path=self.data)

    def symbol_info_tick(self, symbol):
        return self.quote

    def event(self, advance=0.0):
        self.mono += advance
        epoch = int((1700000000 + self.mono + self.offset) * 1000)
        self.quote = SimpleNamespace(time=epoch // 1000, time_msc=epoch, bid=1.1, ask=1.2, flags=6)
        return self.quote

    def copy_rates_from_pos(self, symbol, timeframe, pos, count):
        self.proofs += 1
        return [{"time": self.quote.time - 5}]

    def witnesses(self):
        utc = 1700000000 + self.mono
        return (NTPClockSample(utc, self.mono, .1, "ntp:one"),
                NTPClockSample(utc + self.witness_shift, self.mono, .1, "ntp:two"))


@pytest.fixture
def pair(monkeypatch):
    broker = Terminal()
    monkeypatch.setattr(adapter, "mt5", broker)
    monkeypatch.setattr(adapter, "_MT5_AVAILABLE", True)
    monkeypatch.setattr(adapter._time, "monotonic", lambda: broker.mono)
    monkeypatch.setattr(adapter, "terminal_process_for", lambda path, _previous: (path, broker.process))
    monkeypatch.setattr(adapter, "query_ntp_sources", broker.witnesses)
    connector = adapter.MT5Connector()
    connector._connected = True
    connector.timeframe_map = lambda _tf: 1
    return broker, connector


def synchronize(broker, connector):
    assert connector._read_server_clock_state(broker.event(), "X") is None
    return connector._read_server_clock_state(broker.event(0.2), "X")


@pytest.mark.parametrize("field,value", [
    ("login", 33), ("server", "Other-Demo"), ("data", r"C:\other"), ("process", 2),
])
def test_changed_terminal_lifetime_invalidates_clock_and_m1_immediately(pair, field, value):
    broker, connector = pair
    state = synchronize(broker, connector)
    first = connector._bar_time_authority("X")
    assert first["domain"] == "broker_server"
    assert first["clock_sources"] == ["ntp:one", "ntp:two"]
    assert connector._bar_time_authority("X") == first
    assert broker.proofs == 1
    setattr(broker, field, value)
    assert connector._bar_time_authority("X")["domain"] is None
    assert connector.clock_status()["offset_seconds"] is None
    recovered = connector._read_server_clock_state(broker.event(.25), "X")
    assert recovered.generation > state.generation
    assert (recovered.terminal_fingerprint == state.terminal_fingerprint) == (field == "process")
    assert connector._bar_time_authority("X")["clock_generation"] == recovered.generation
    assert broker.proofs == 2


def test_observed_disconnect_and_reconnect_cannot_reuse_same_account_proof(pair):
    broker, connector = pair
    previous = synchronize(broker, connector)
    connector._bar_time_authority("X")
    broker.connected = False
    assert connector.is_connected() is False
    assert connector.clock_status()["phase"] == "disconnected"
    broker.connected = True
    assert connector.is_connected() is True
    assert connector._bar_time_authority("X")["domain"] is None
    recovered = connector._read_server_clock_state(broker.event(.25), "X")
    assert recovered.generation > previous.generation
    assert recovered.terminal_fingerprint == previous.terminal_fingerprint


def test_disagreement_preserves_bounded_holdover_then_expires_and_recovers(pair):
    broker, connector = pair
    anchor = synchronize(broker, connector)
    connector._bar_time_authority("X")
    broker.mono += 9.9
    connector._read_server_clock_state(broker.quote, "X")
    broker.witness_shift = 20.0
    view = connector._read_server_clock_state(broker.event(.3), "X")
    assert view.synchronization_status == "holdover"
    assert view.observed_monotonic == anchor.observed_monotonic
    assert view.uncertainty_seconds > anchor.uncertainty_seconds
    assert connector.clock_status()["reason"] == "utc_witnesses_not_corroborated"
    broker.mono = anchor.observed_monotonic + CLOCK_MAX_AGE_SECONDS + .01
    assert connector._bar_time_authority("X")["domain"] is None
    assert connector.clock_status()["phase"] == "expired"
    broker.witness_shift = 0
    restored = connector._read_server_clock_state(broker.event(.25), "X")
    assert restored.observed_monotonic > anchor.observed_monotonic
    assert connector._bar_time_authority("X")["domain"] == "broker_server"


def test_holdover_uncertainty_can_expire_before_maximum_age(pair):
    broker, connector = pair
    anchor = synchronize(broker, connector)
    connector._server_clock_state_cache = replace(anchor, uncertainty_seconds=2.499)
    broker.mono += 2.0
    assert connector._read_server_clock_state(broker.quote, "X") is None
    assert connector.clock_status()["phase"] == "expired"


@pytest.mark.parametrize("step", [-3600, 3600])
def test_discontinuous_offset_requires_new_witness_and_bar_proof(pair, step):
    broker, connector = pair
    anchor = synchronize(broker, connector)
    old = connector._bar_time_authority("X")
    broker.offset += step
    assert connector._read_server_clock_state(broker.event(.1), "X") is None
    assert connector._bar_time_authority("X")["domain"] is None
    fresh = connector._read_server_clock_state(broker.event(.2), "X")
    assert fresh.generation > anchor.generation
    new = connector._bar_time_authority("X")
    assert new["tested_offset_seconds"] == pytest.approx(old["tested_offset_seconds"] + step)
    assert broker.proofs == 2


def test_identity_switch_during_utc_query_discards_proof(pair, monkeypatch):
    broker, connector = pair
    assert connector._read_server_clock_state(broker.event(), "X") is None

    def change_account():
        broker.login += 1
        return broker.witnesses()

    monkeypatch.setattr(adapter, "query_ntp_sources", change_account)
    assert connector._read_server_clock_state(broker.event(.25), "X") is None
    assert connector.clock_status()["offset_seconds"] is None


@pytest.mark.parametrize("result", ["match", "repeated", "different_tick", "different_epoch", "outside_bounds"])
def test_nonadvancing_latest_requires_single_exact_event_inside_utc_history(pair, result):
    broker, connector = pair
    broker.offset = 0
    quote = broker.event()
    row = vars(quote).copy()

    def history(symbol, start, end, flags):
        assert start.timestamp() <= row["time_msc"] / 1000 <= end.timestamp()
        if result == "repeated":
            return [row, row]
        if result == "different_tick":
            return [dict(row, bid=1.11)]
        if result == "different_epoch":
            return [dict(row, time_msc=row["time_msc"] - 1)]
        if result == "outside_bounds":
            return [dict(row, time_msc=1), row]
        return [row]

    broker.copy_ticks_range = history
    state = connector._read_server_clock_state(quote, "X")
    if result == "match":
        assert state.observation_basis == "exact_latest_event_in_utc_history"
        assert state.offset_seconds == 0
        assert sorted(state.sources) == ["ntp:one", "ntp:two"]
    else:
        assert state is None


def test_witness_acquisition_that_outlives_holdover_is_discarded(pair, monkeypatch):
    broker, connector = pair
    assert connector._read_server_clock_state(broker.event(), "X") is None

    def stall():
        samples = broker.witnesses()
        broker.mono += CLOCK_MAX_AGE_SECONDS + 1
        return samples

    monkeypatch.setattr(adapter, "query_ntp_sources", stall)
    assert connector._read_server_clock_state(broker.event(.25), "X") is None
    assert connector.clock_status()["phase"] == "expired"


def test_terminal_process_witness_uses_lifetime_and_exact_install_path(monkeypatch):
    def process(path):
        return SimpleNamespace(info={"name": "terminal64.exe"}, exe=lambda: path,
                               create_time=lambda: 1700000000, is_running=lambda: True)
    old = process(r"C:\one\terminal64.exe")
    other = process(r"C:\two\terminal64.exe")
    new = process(r"C:\one\terminal64.exe")
    processes = [old, other]
    calls = []

    def lookup(fields):
        calls.append(tuple(fields))
        return list(processes)

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        process_iter=lookup, Error=RuntimeError, NoSuchProcess=LookupError))
    witness = terminal_process_for(r"C:\one")
    assert witness[1] is old
    assert terminal_process_for(r"C:\one", witness) is witness
    assert calls == [("name",)]
    old.is_running = lambda: False
    processes[:] = [other, new]
    assert terminal_process_for(r"C:\one", witness)[1] is new
    processes.append(process(r"C:\one\terminal64.exe"))
    assert terminal_process_for(r"C:\one") is None


def test_status_keeps_sync_transition_and_rediscovers_process_on_account_change(pair, monkeypatch):
    broker, connector = pair
    synchronize(broker, connector)
    assert connector.clock_status()["phase"] == "synchronized"
    previous_values = []
    monkeypatch.setattr(adapter, "terminal_process_for",
                        lambda path, previous: previous_values.append(previous) or (path, broker.process))
    broker.login += 1
    assert connector._read_server_clock_state(broker.quote, "X") is None
    assert previous_values == [None]


def test_m1_fetch_that_outlives_clock_cannot_publish_a_bar_proof(pair):
    broker, connector = pair
    synchronize(broker, connector)
    original = broker.copy_rates_from_pos

    def delayed_m1(*args):
        broker.mono += CLOCK_MAX_AGE_SECONDS + 1
        return original(*args)

    broker.copy_rates_from_pos = delayed_m1
    proof = connector._bar_time_authority("X")
    assert proof["domain"] is None
    assert proof["reason"] == "clock expired or changed during M1 proof"
    assert connector.clock_status()["phase"] == "expired"

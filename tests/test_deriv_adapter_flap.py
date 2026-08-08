"""Regression tests for the DerivTickAdapter connection-flap fix.

A flaky Deriv WebSocket used to flip the disconnect/recover log lines once per
symbol inside a single poll cycle, flooding the log with ~15 lines per 100ms.
The connection state is now evaluated once per full symbol sweep with
hysteresis: recovery is only declared after a fully clean cycle, and mixed
cycles (some symbols OK, some failing) hold the current state without flapping.
"""

from __future__ import annotations

from types import SimpleNamespace

from loguru import logger

from tick import EventBus, TickStore, CandleCloseDetector, TickRouter


def _make_adapter(symbols, results):
    """Build a DerivTickAdapter whose get_price replays a scripted result list.

    `results` is a flat list of per-call behaviors ("ok" | "none" | "conn").
    Each poll cycle consumes len(symbols) entries (one per symbol, in order),
    so len(results) must be a multiple of len(symbols). Running is flipped off
    on the final call — which is always the last symbol of the last cycle — so
    every cycle completes fully before the loop exits.
    """
    from event_driven_bootstrap import DerivTickAdapter

    bus = EventBus()
    store = TickStore()
    detector = CandleCloseDetector(bus)
    router = TickRouter(store, detector, bus)

    holder: list = [None]
    state = {"n": 0}
    total = len(results)

    def get_price(sym):
        i = state["n"]
        state["n"] += 1
        if i >= total - 1 and holder[0] is not None:
            holder[0]._running = False
        behavior = results[i] if i < total else "conn"
        if behavior == "ok":
            return SimpleNamespace(bid=1.1000, ask=1.1002)
        if behavior == "none":
            return None
        raise Exception(f"Deriv not connected for {sym}")

    pm = SimpleNamespace(get_price=get_price)
    adapter = DerivTickAdapter(pm, router, list(symbols), poll_interval=0.0)
    holder[0] = adapter
    return adapter, store


def _run_capture(adapter):
    """Run _poll_loop synchronously, capturing loguru messages."""
    messages: list[str] = []
    sink_id = logger.add(
        lambda m: messages.append(m.record["message"]), level="DEBUG",
    )
    try:
        adapter._running = True
        adapter._poll_loop()
    finally:
        logger.remove(sink_id)
    return messages


def _count(messages, needle):
    return sum(1 for m in messages if needle in m)


class TestDerivAdapterFlap:
    def test_all_fail_cycle_declares_disconnect_once(self):
        adapter, _ = _make_adapter(["A", "B"], ["conn", "conn"])
        msgs = _run_capture(adapter)
        assert _count(msgs, "Deriv disconnected") == 1
        assert _count(msgs, "connection recovered") == 0

    def test_clean_cycle_after_outage_recovers_once(self):
        # Cycle 1: all conn errors → down. Cycle 2: all ok → recovered.
        adapter, store = _make_adapter(
            ["A", "B"], ["conn", "conn", "ok", "ok"],
        )
        msgs = _run_capture(adapter)
        assert _count(msgs, "Deriv disconnected") == 1
        assert _count(msgs, "connection recovered") == 1
        # Ticks from the clean cycle still flowed through the router.
        assert store.get_latest("A") is not None
        assert store.get_latest("B") is not None

    def test_mixed_cycle_while_down_holds_state(self):
        # Cycle 1: all conn → down. Cycle 2: mixed (ok + conn) → must NOT recover.
        adapter, _ = _make_adapter(
            ["A", "B"], ["conn", "conn", "ok", "conn"],
        )
        msgs = _run_capture(adapter)
        assert _count(msgs, "Deriv disconnected") == 1
        assert _count(msgs, "connection recovered") == 0

    def test_mixed_cycle_while_up_holds_state(self):
        # Starting healthy, a mixed cycle must NOT declare a disconnect.
        adapter, _ = _make_adapter(["A", "B"], ["ok", "conn"])
        msgs = _run_capture(adapter)
        assert _count(msgs, "Deriv disconnected") == 0
        assert _count(msgs, "connection recovered") == 0

    def test_rapid_mixed_cycles_do_not_flap(self):
        # 10 consecutive mixed cycles (each has a good tick AND a conn error).
        # The pre-fix code flipped the state up to once per symbol per cycle;
        # the fix must emit zero disconnect/recover lines across all of them.
        results = ["ok", "conn"] * 10
        adapter, _ = _make_adapter(["A", "B"], results)
        msgs = _run_capture(adapter)
        assert _count(msgs, "Deriv disconnected") <= 1
        assert _count(msgs, "connection recovered") <= 1
        # Specifically: every cycle is mixed, so state never changes.
        assert _count(msgs, "Deriv disconnected") == 0
        assert _count(msgs, "connection recovered") == 0

    def test_ticks_still_route_during_mixed_cycle(self):
        # A good tick within a mixed cycle must still reach the router.
        adapter, store = _make_adapter(["A", "B"], ["ok", "conn"])
        _run_capture(adapter)
        latest = store.get_latest("A")
        assert latest is not None
        assert latest.source == "deriv"

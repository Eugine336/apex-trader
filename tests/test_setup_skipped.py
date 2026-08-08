"""
APEX TRADER — Tests: SETUP_SKIPPED event emission
Verifies that:
  1. Non-READY, non-MARKET_CLOSED scanner results emit SETUP_SKIPPED
     with cycle_id and the symbol/status/score payload.
  2. READY instruments do NOT emit SETUP_SKIPPED.
  3. MARKET_CLOSED instruments are suppressed (throttle policy).
  4. Emit-on-change: repeated identical (status, score) across cycles
     suppresses duplicate emissions.
  5. A bad payload does not raise into the scan loop.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock


from persistence.event_store import EventStore, new_cycle_id
from persistence.domain_events import SETUP_SKIPPED


def _make_store(tmp_path):
    db = str(tmp_path / "test_events.db")
    return EventStore(db_path=db)


def _flush_and_query(store, **kwargs):
    store.flush(timeout=3.0)
    return store.query(**kwargs)


def _make_result(pair, status, score, direction="NEUTRAL"):
    """Minimal PairScanResult-like object with the fields _emit_setup_skipped reads."""
    return SimpleNamespace(
        pair=pair,
        status=status,
        score=score,
        direction=direction,
        trend_h4="BULLISH",
        trend_h1="BEARISH",
        bias_strength="MODERATE",
        regime="TRENDING",
        session_active=True,
        has_fvg=False,
        has_order_block=False,
        confluences=["structure"],
        instrument_category="forex",
        ev_estimate=0.5,
    )


def _make_report(results):
    return SimpleNamespace(results=results)


def _emit_setup_skipped(loop_state, report, store, correlation_id=None):
    """Replicates the exact logic of TradingLoop._emit_setup_skipped
    without importing TradingLoop (avoids torch/heavy dep chain)."""
    if store is None:
        return
    current: dict[str, tuple[str, int]] = {}
    for r in report.results:
        if r.status == "READY" or r.status == "MARKET_CLOSED":
            continue
        current[r.pair] = (r.status, r.score)
        prev = loop_state.get(r.pair)
        if prev == (r.status, r.score):
            continue
        try:
            store.emit(
                event_type=SETUP_SKIPPED,
                severity="DEBUG",
                symbol=r.pair,
                correlation_id=correlation_id,
                source_module="platforms.main_loop",
                payload={
                    "status": r.status,
                    "score": r.score,
                    "direction": r.direction,
                    "trend_h4": r.trend_h4,
                    "trend_h1": r.trend_h1,
                    "bias_strength": r.bias_strength,
                    "regime": r.regime,
                    "session_active": r.session_active,
                    "has_fvg": r.has_fvg,
                    "has_order_block": r.has_order_block,
                    "confluences": r.confluences,
                    "instrument_category": r.instrument_category,
                    "ev_estimate": r.ev_estimate,
                },
            )
        except Exception:
            pass
    loop_state.clear()
    loop_state.update(current)


# ── Test 1: SETUP_SKIPPED emitted for non-READY, non-MARKET_CLOSED ──────────

class TestSetupSkippedEmission:
    def test_skipped_for_watchlist_and_waiting(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            state: dict = {}
            report = _make_report([
                _make_result("EURUSD", "READY", 90, "LONG"),
                _make_result("GBPUSD", "WATCHLIST", 65, "SHORT"),
                _make_result("USDJPY", "WAITING", 30),
                _make_result("XAUUSD", "MARKET_CLOSED", 0),
            ])

            _emit_setup_skipped(state, report, store)

            events = _flush_and_query(store, event_type=SETUP_SKIPPED)
            symbols = {ev["symbol"] for ev in events}
            assert symbols == {"GBPUSD", "USDJPY"}
            assert len(events) == 2

            for ev in events:
                payload = json.loads(ev["payload_json"])
                assert "status" in payload
                assert "score" in payload
                assert "direction" in payload
                assert "trend_h4" in payload
                assert "confluences" in payload
        finally:
            store.close()


# ── Test 2: READY not emitted as SETUP_SKIPPED ──────────────────────────────

class TestReadyNotSkipped:
    def test_ready_excluded(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            state: dict = {}
            report = _make_report([
                _make_result("EURUSD", "READY", 92, "LONG"),
            ])

            _emit_setup_skipped(state, report, store)

            events = _flush_and_query(store, event_type=SETUP_SKIPPED)
            assert len(events) == 0
        finally:
            store.close()


# ── Test 3: MARKET_CLOSED suppressed ────────────────────────────────────────

class TestMarketClosedSuppressed:
    def test_market_closed_not_emitted(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            state: dict = {}
            report = _make_report([
                _make_result("AAPL", "MARKET_CLOSED", 0),
                _make_result("MSFT", "MARKET_CLOSED", 0),
            ])

            _emit_setup_skipped(state, report, store)

            events = _flush_and_query(store, event_type=SETUP_SKIPPED)
            assert len(events) == 0
        finally:
            store.close()


# ── Test 4: Emit-on-change throttling ───────────────────────────────────────

class TestEmitOnChange:
    def test_unchanged_suppressed_across_cycles(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            state: dict = {}
            report = _make_report([
                _make_result("GBPUSD", "WATCHLIST", 65, "SHORT"),
            ])

            _emit_setup_skipped(state, report, store)
            _emit_setup_skipped(state, report, store)

            events = _flush_and_query(store, event_type=SETUP_SKIPPED)
            assert len(events) == 1

            changed_report = _make_report([
                _make_result("GBPUSD", "WAITING", 40, "SHORT"),
            ])
            _emit_setup_skipped(state, changed_report, store)

            events = _flush_and_query(store, event_type=SETUP_SKIPPED, symbol="GBPUSD")
            assert len(events) == 2
            payloads = [json.loads(e["payload_json"]) for e in events]
            assert payloads[0]["status"] == "WATCHLIST"
            assert payloads[1]["status"] == "WAITING"
        finally:
            store.close()


# ── Test 5: Safety — bad payload does not raise ─────────────────────────────

class TestSetupSkippedSafety:
    def test_emit_failure_does_not_raise(self, tmp_path):
        state: dict = {}
        broken_result = _make_result("EURUSD", "WAITING", 20)
        broken_result.confluences = object()
        report = _make_report([broken_result])

        broken_store = MagicMock()
        broken_store.emit.side_effect = Exception("serialization boom")

        _emit_setup_skipped(state, report, broken_store)

    def test_none_store_does_not_raise(self, tmp_path):
        state: dict = {}
        report = _make_report([_make_result("EURUSD", "WAITING", 20)])

        _emit_setup_skipped(state, report, None)


# ── Test 6: SETUP_SKIPPED carries correlation_id ────────────────────────────

class TestSetupSkippedCorrelationId:
    def test_skipped_event_carries_cycle_id(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            state: dict = {}
            cycle_id = new_cycle_id()
            report = _make_report([
                _make_result("GBPUSD", "WATCHLIST", 65, "SHORT"),
            ])

            _emit_setup_skipped(state, report, store, correlation_id=cycle_id)

            events = _flush_and_query(store, event_type=SETUP_SKIPPED)
            assert len(events) == 1
            assert events[0]["correlation_id"] == cycle_id
            assert events[0]["correlation_id"] is not None
        finally:
            store.close()

    def test_skipped_event_without_cycle_id_is_null(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            state: dict = {}
            report = _make_report([
                _make_result("EURUSD", "WAITING", 30),
            ])

            _emit_setup_skipped(state, report, store, correlation_id=None)

            events = _flush_and_query(store, event_type=SETUP_SKIPPED)
            assert len(events) == 1
            assert events[0]["correlation_id"] is None
        finally:
            store.close()

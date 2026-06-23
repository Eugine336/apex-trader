"""Tests for the NewsImpactTracker measurement trigger.

The tracker captures price at a fired high-impact news event and, once
``measure_after_min`` has elapsed, feeds the realised reaction (in pips,
normalised by ATR downstream) to ``CalibrationEngine.record_news_impact``.
Pending baselines persist to JSON so a restart inside the window keeps them.
"""

from datetime import datetime, timedelta, timezone

from brain.news_impact_tracker import NewsImpactTracker


class _FakeStats:
    def atr_pips(self, tf="M5"):
        return 20.0


class _FakeCalib:
    """Stands in for CalibrationEngine: provider hook + record sink."""

    def __init__(self):
        self.calls = []

    def __call__(self, symbol):
        return _FakeStats()

    def record_news_impact(self, symbol, currency, move_pips, atr_pips):
        self.calls.append((symbol, currency, move_pips, atr_pips))


class _Event:
    def __init__(self, currency, time_utc, impact="HIGH"):
        self.currency = currency
        self.time_utc = time_utc
        self.impact = impact


_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_capture_then_measure_after_30min(tmp_path):
    calib = _FakeCalib()
    trk = NewsImpactTracker(
        calib, news_guard=None, state_path=str(tmp_path / "pending.json"),
    )
    ev = _Event("USD", _T0)

    # Capture baseline 2 min after the event fires.
    trk.on_m5_close("EURUSD", 1.1000, now=_T0 + timedelta(minutes=2), events=[ev])
    assert len(trk._pending) == 1

    # Still inside the window at 20 min → no measurement yet.
    trk.on_m5_close("EURUSD", 1.1010, now=_T0 + timedelta(minutes=20), events=[])
    assert calib.calls == []

    # Matured at 31 min: |1.1030 - 1.1000| / 0.0001 = 30 pips, ATR 20.
    trk.on_m5_close("EURUSD", 1.1030, now=_T0 + timedelta(minutes=31), events=[])
    assert len(calib.calls) == 1
    symbol, currency, move_pips, atr_pips = calib.calls[0]
    assert symbol == "EURUSD"
    assert currency == "USD"
    assert move_pips == 30.0
    assert atr_pips == 20.0
    assert trk._pending == {}


def test_non_high_impact_is_ignored(tmp_path):
    calib = _FakeCalib()
    trk = NewsImpactTracker(
        calib, news_guard=None, state_path=str(tmp_path / "pending.json"),
    )
    trk.on_m5_close(
        "EURUSD", 1.1, now=_T0 + timedelta(minutes=2),
        events=[_Event("USD", _T0, impact="MEDIUM")],
    )
    assert trk._pending == {}


def test_duplicate_event_captured_once(tmp_path):
    calib = _FakeCalib()
    trk = NewsImpactTracker(
        calib, news_guard=None, state_path=str(tmp_path / "pending.json"),
    )
    ev = _Event("USD", _T0)
    trk.on_m5_close("EURUSD", 1.1000, now=_T0 + timedelta(minutes=2), events=[ev])
    trk.on_m5_close("EURUSD", 1.1005, now=_T0 + timedelta(minutes=4), events=[ev])
    assert len(trk._pending) == 1


def test_pending_persists_across_restart(tmp_path):
    state = str(tmp_path / "pending.json")
    calib = _FakeCalib()
    trk = NewsImpactTracker(calib, news_guard=None, state_path=state)
    trk.on_m5_close(
        "EURUSD", 1.2000, now=_T0 + timedelta(minutes=1), events=[_Event("EUR", _T0)],
    )
    trk.save()

    reloaded = NewsImpactTracker(calib, news_guard=None, state_path=state)
    assert len(reloaded._pending) == 1


def test_stale_pending_expires(tmp_path):
    calib = _FakeCalib()
    trk = NewsImpactTracker(
        calib, news_guard=None, state_path=str(tmp_path / "pending.json"),
        max_pending_age_min=180.0,
    )
    trk.on_m5_close("EURUSD", 1.1, now=_T0 + timedelta(minutes=2), events=[_Event("USD", _T0)])
    # A much later close measures (>=30 min) and clears the entry.
    trk.on_m5_close("EURUSD", 1.1, now=_T0 + timedelta(minutes=300), events=[])
    assert trk._pending == {}


def test_zero_price_is_noop(tmp_path):
    calib = _FakeCalib()
    trk = NewsImpactTracker(
        calib, news_guard=None, state_path=str(tmp_path / "pending.json"),
    )
    trk.on_m5_close("EURUSD", 0.0, now=_T0 + timedelta(minutes=2), events=[_Event("USD", _T0)])
    assert trk._pending == {}

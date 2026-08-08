"""Structure engine — the "not enough candles" notice must be ATTRIBUTABLE and
THROTTLED.

The engine is a shared singleton reused across instruments/timeframes (see
``scanner/candle_close_handler``), so a thin (symbol, timeframe) previously
re-warned on every candle close with an anonymous message. The notice now names
the starved SYMBOL/TF plus have/need, warns ONCE per context, then drops to
DEBUG — so a persistent gap is diagnosable while a benign cold-start shortfall
does not flood the logs.
"""

import brain.structure_engine as se
from brain.structure_engine import StructureEngine, StructureEvent, Trend


class _RecordingLogger:
    def __init__(self):
        self.warnings = []
        self.debugs = []

    def warning(self, msg, *args):
        self.warnings.append((msg, args))

    def debug(self, msg, *args):
        self.debugs.append((msg, args))

    def __getattr__(self, _n):
        def _f(*a, **k):
            return None
        return _f


def _short(n):
    # A plain list suffices: the starvation branch only calls len(df) before
    # returning _empty_analysis(), never touching pandas.
    return list(range(n))


def test_short_frame_returns_empty_analysis():
    eng = StructureEngine(swing_lookback=5)          # need = 5*2+1 = 11
    out = eng.analyze(_short(6), context="BTCUSD/D1")
    assert out.trend is Trend.RANGING
    assert out.last_event is StructureEvent.NONE
    assert out.confidence == 0.0


def test_notice_names_symbol_tf_have_and_need(monkeypatch):
    rec = _RecordingLogger()
    monkeypatch.setattr(se, "logger", rec)
    eng = StructureEngine(swing_lookback=5)          # need = 11
    eng.analyze(_short(4), context="BTCUSD/D1")
    assert len(rec.warnings) == 1
    _msg, args = rec.warnings[0]
    assert args == ("BTCUSD/D1", 4, 11)              # label, have, need


def test_notice_is_throttled_per_context(monkeypatch):
    rec = _RecordingLogger()
    monkeypatch.setattr(se, "logger", rec)
    eng = StructureEngine(swing_lookback=3)          # need = 7
    for _ in range(3):
        eng.analyze(_short(2), context="ETHUSD/H4")  # same context ×3
    assert len(rec.warnings) == 1                    # warned once
    assert len(rec.debugs) == 2                      # repeats fell to DEBUG
    eng.analyze(_short(2), context="XAUUSD/H1")      # a NEW context warns again
    assert len(rec.warnings) == 2


def test_missing_context_uses_placeholder(monkeypatch):
    rec = _RecordingLogger()
    monkeypatch.setattr(se, "logger", rec)
    eng = StructureEngine(swing_lookback=5)
    eng.analyze(_short(1))                           # no context passed
    assert rec.warnings[0][1][0] == "?"

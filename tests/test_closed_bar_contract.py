"""Closed-bar contract enforcement for the HTF brain pipeline.

Structural modules (FVG, OrderBlock, Liquidity, Wyckoff, Inducement) must
reason over CLOSED candles only — never the still-forming bar — while
observational reads (Volume, Concepts) and the self-dropping StructureEngine
keep the live frame.  See ``brain/market_data_utils.py`` for the contract.
"""

from types import SimpleNamespace

import pandas as pd

import brain.decision_core as dc
from brain.wyckoff_engine import WyckoffEngine


def _ohlc(n: int) -> pd.DataFrame:
    closes = [1.1000 + i * 0.0010 for i in range(n)]
    opens = [closes[0]] + closes[:-1]
    highs = [c + 0.0008 for c in closes]
    lows = [c - 0.0008 for c in closes]
    return pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [100.0 + i for i in range(n)],
        }
    )


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        fvg_proximity_pips=10.0,
        fvg_min_size_pips=1.0,
        ob_min_impulse_pips=5.0,
        ob_buffer_pips=2.0,
        wyckoff_enabled=True,
    )


class _RecordMap:
    def __init__(self) -> None:
        self.seen: int | None = None

    def map(self, df, pip_size):
        self.seen = len(df)
        return {}


class _RecordVolume:
    def __init__(self) -> None:
        self.seen: int | None = None
        self.closed_seen: int | None = None

    def analyze(self, df, closed_df=None):
        self.seen = len(df)
        self.closed_seen = len(closed_df) if closed_df is not None else None
        return {}


class _RecordStructure:
    def __init__(self) -> None:
        self.seen: int | None = None

    def analyze(self, df, pip_size=None, context=None):
        self.seen = len(df)
        # Current WyckoffEngine consumes the structure result's trend field.
        return SimpleNamespace(trend="up", structure="higher_highs")


def test_structural_modules_get_closed_bar_observational_get_live(monkeypatch):
    """FVG/OB/Liquidity drop the forming bar; Volume/Structure keep it."""
    n = 60
    df = _ohlc(n)

    monkeypatch.setattr(dc, "get_pip_size", lambda symbol: 0.0001)
    monkeypatch.setattr(dc, "get_profile", lambda symbol: _profile())

    fvg_seen: dict[str, int] = {}
    ob_seen: dict[str, int] = {}

    def _fvg_detect(self, d, timeframe=None):
        fvg_seen["n"] = len(d)
        return []

    def _ob_detect(self, d, timeframe=None):
        ob_seen["n"] = len(d)
        return []

    monkeypatch.setattr(dc.FVGDetector, "detect", _fvg_detect)
    monkeypatch.setattr(dc.OrderBlockDetector, "detect", _ob_detect)

    liquidity = _RecordMap()
    volume = _RecordVolume()
    structure = _RecordStructure()

    dc.run_tf_modules(
        "EURUSD", "H1", df,
        structure=structure, liquidity=liquidity, volume=volume,
    )

    # Structural artifacts → closed bar (forming bar removed).
    assert fvg_seen["n"] == n - 1
    assert ob_seen["n"] == n - 1
    assert liquidity.seen == n - 1
    # Volume receives BOTH views: the live frame for observational reads and
    # the closed frame for structural verdicts (dual-output contract).
    assert volume.seen == n
    assert volume.closed_seen == n - 1
    # Self-dropping StructureEngine → live frame.
    assert structure.seen == n


def test_inducement_gets_closed_bar(monkeypatch):
    """Inducement (trap classification) must drop the forming bar."""
    n = 60
    df = _ohlc(n)

    monkeypatch.setattr(dc, "get_pip_size", lambda symbol: 0.0001)
    monkeypatch.setattr(dc, "get_profile", lambda symbol: _profile())

    ind_seen: dict[str, int] = {}

    def _ind_analyze(self, d):
        ind_seen["n"] = len(d)
        return []

    monkeypatch.setattr(dc.InducementDetector, "analyze", _ind_analyze)

    dc.run_tf_modules(
        "EURUSD", "M5", df,
        structure=_RecordStructure(), liquidity=_RecordMap(), volume=_RecordVolume(),
    )

    assert ind_seen["n"] == n - 1


def test_volume_dual_output_structural_from_closed():
    """Volume keeps live observation but derives structural verdicts from closed.

    A forming-bar volume spike must NOT trip ``has_spike`` (a structural
    verdict) when a closed frame is supplied, while ``volume_ratio`` (the live
    observation) still reflects the developing bar.
    """
    from brain.volume_analyzer import VolumeAnalyzer
    from brain.market_data_utils import drop_forming_bar

    n = 40
    flat = [100.0] * n
    vols = [100.0] * (n - 1) + [1000.0]  # huge partial spike on the forming bar
    df = pd.DataFrame(
        {
            "open": flat,
            "high": [c + 1 for c in flat],
            "low": [c - 1 for c in flat],
            "close": flat,
            "volume": vols,
        }
    )
    closed = drop_forming_bar(df)
    va = VolumeAnalyzer()

    legacy = va.analyze(df)                       # structural read off forming bar
    dual = va.analyze(df, closed_df=closed)        # structural read off closed bar

    assert legacy.has_spike is True               # phantom spike trips the old path
    assert dual.has_spike is False                # closed-bar structural ignores it
    assert dual.volume_ratio == legacy.volume_ratio  # live observation preserved


def test_wyckoff_no_double_drop():
    """Wyckoff hands its inner StructureEngine the LIVE frame (single drop).

    The inner StructureEngine self-drops the forming bar internally; if Wyckoff
    pre-dropped, the inner engine would double-drop and lose the most recent
    closed bar.  A 10-bar live frame must reach the inner engine as 10 bars so
    it processes 9 closed bars — not 8.
    """
    n = 50
    df = _ohlc(n)

    engine = WyckoffEngine(pip_size=0.0001)
    rec = _RecordStructure()
    engine.structure_engine = rec

    engine.analyze(df)

    # Inner engine receives the full live frame — it self-drops to n-1 closed.
    assert rec.seen == n


def test_wyckoff_spring_and_upthrust_are_mirrored_without_symbol_bias():
    rows = [{
        "open": 100.0, "high": 110.0, "low": 90.0,
        "close": 100.0, "volume": 100.0,
    } for _ in range(39)]
    rows.append({
        "open": 95.0, "high": 98.0, "low": 89.0,
        "close": 94.0, "volume": 100.0,
    })
    original = pd.DataFrame(rows)
    mirrored = original.copy()
    mirrored["open"] = 200.0 - original["open"]
    mirrored["close"] = 200.0 - original["close"]
    # Reflection swaps extrema: new high is reflected old low, and vice versa.
    mirrored["high"] = 200.0 - original["low"]
    mirrored["low"] = 200.0 - original["high"]
    engine = WyckoffEngine(pip_size=0.01)
    high, low, _ = engine._range_state(original)
    m_high, m_low, _ = engine._range_state(mirrored)
    assert (high, low) == (110.0, 90.0)
    assert (m_high, m_low) == (110.0, 90.0)
    assert engine._is_spring(original, low)
    assert not engine._is_upthrust(original, high)
    assert not engine._is_spring(mirrored, m_low)
    assert engine._is_upthrust(mirrored, m_high)


def test_wyckoff_reference_range_excludes_candidate_bar():
    rows = [{
        "open": 100.0, "high": 110.0, "low": 90.0,
        "close": 100.0, "volume": 100.0,
    } for _ in range(39)]
    rows.append({
        "open": 95.0, "high": 98.0, "low": 89.0,
        "close": 94.0, "volume": 100.0,
    })
    frame = pd.DataFrame(rows)
    engine = WyckoffEngine(pip_size=0.01)
    high, low, _ = engine._range_state(frame)
    assert (high, low) == (110.0, 90.0)
    assert engine._is_spring(frame, low)

import pandas as pd

from brain.structure_engine import StructureEngine, StructureEvent, Trend


def _build_ohlc(closes: list[float]) -> pd.DataFrame:
    opens = [closes[0]] + closes[:-1]
    highs = [c + 0.0008 for c in closes]
    lows = [c - 0.0008 for c in closes]
    return pd.DataFrame(
        {
            "time": pd.date_range(
                "2026-01-01", periods=len(closes), freq="5min", tz="UTC"
            ),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
        }
    )


def test_structure_engine_returns_empty_analysis_for_short_data():
    engine = StructureEngine(swing_lookback=3)
    df = _build_ohlc([1.1000, 1.1002, 1.1001, 1.1003, 1.1002])

    analysis = engine.analyze(df)

    assert analysis.trend == Trend.RANGING
    assert analysis.last_event == StructureEvent.NONE
    assert analysis.confidence == 0.0


def test_structure_engine_bias_payload_has_expected_fields():
    closes = [
        1.1000,
        1.1010,
        1.1004,
        1.1018,
        1.1012,
        1.1024,
        1.1018,
        1.1030,
        1.1022,
        1.1036,
        1.1028,
        1.1042,
        1.1034,
        1.1048,
        1.1040,
        1.1052,
        1.1044,
    ]
    df = _build_ohlc(closes)
    engine = StructureEngine(swing_lookback=1)

    bias = engine.get_bias(df, df)

    assert bias["direction"] in {
        Trend.BULLISH.value,
        Trend.BEARISH.value,
        Trend.RANGING.value,
    }
    assert bias["strength"] in {"STRONG", "MODERATE", "CONFLICTED", "NONE"}
    assert isinstance(bias["confidence"], float)
    assert "tradeable" in bias

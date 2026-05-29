import pandas as pd

from brain.fvg_detector import FVGDetector


def test_fvg_detector_finds_bullish_gap():
    df = pd.DataFrame(
        {
            "time": pd.date_range("2026-01-02", periods=5, freq="5min", tz="UTC"),
            "open": [1.1000, 1.1003, 1.1015, 1.1019, 1.1022],
            "high": [1.1004, 1.1016, 1.1020, 1.1023, 1.1028],
            "low": [1.0996, 1.1001, 1.1010, 1.1015, 1.1019],
            "close": [1.1002, 1.1014, 1.1018, 1.1021, 1.1026],
        }
    )

    detector = FVGDetector(min_size_pips=2.0, pip_size=0.0001)
    fvgs = detector.detect(df, timeframe="M5")

    assert fvgs
    assert any(f.kind == "BULLISH" for f in fvgs)


def test_fvg_detector_gets_entry_fvg_for_long_direction():
    df = pd.DataFrame(
        {
            "time": pd.date_range("2026-01-03", periods=6, freq="5min", tz="UTC"),
            "open": [1.2000, 1.2003, 1.2016, 1.2020, 1.2018, 1.2022],
            "high": [1.2005, 1.2018, 1.2022, 1.2024, 1.2021, 1.2028],
            "low": [1.1998, 1.2001, 1.2011, 1.2017, 1.2014, 1.2020],
            "close": [1.2002, 1.2015, 1.2020, 1.2022, 1.2019, 1.2026],
        }
    )

    detector = FVGDetector(min_size_pips=2.0, pip_size=0.0001)
    fvgs = detector.detect(df, timeframe="M5")
    entry_fvg = detector.get_entry_fvg(fvgs, direction="LONG", current_price=1.2030)

    assert entry_fvg is not None
    assert entry_fvg.kind == "BULLISH"

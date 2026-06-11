import pandas as pd
import pytest

from config import AppConfig
from trigger.entry_engine import EntryEngine


def _make_m5_df(base: float = 1.2700, n: int = 40, half_range: float = 0.0015) -> pd.DataFrame:
    rows = []
    for i in range(n):
        close = base + ((i % 3) - 1) * 0.00001
        rows.append(
            {
                "time": pd.Timestamp("2025-01-01") + pd.Timedelta(minutes=5 * i),
                "open": close,
                "high": close + half_range,
                "low": close - half_range,
                "close": close,
            }
        )
    return pd.DataFrame(rows)


class TestEntryEngineVolatilityStop:
    def setup_method(self):
        self.pip = 0.0001
        self.entry_zone = {"bottom": 1.27000, "top": 1.27020, "midpoint": 1.27010}
        self.entry_price = 1.27010

    def _structure_sl(self) -> float:
        return self.entry_zone["bottom"] - 2 * self.pip

    def test_mode_off_uses_structure_stop_loss(self):
        cfg = AppConfig()
        cfg.risk.volatility_stop_mode = "off"
        engine = EntryEngine(config=cfg)

        result = engine.calculate_stop_loss(
            "LONG",
            self.entry_zone,
            self.pip,
            entry_price=self.entry_price,
            pair="EURUSD",
            m5_df=_make_m5_df(),
        )

        assert result == self._structure_sl()

    def test_mode_on_applies_clamped_atr_stop_loss(self):
        cfg = AppConfig()
        cfg.risk.volatility_stop_mode = "on"
        cfg.risk.atr_stop_period = 14
        cfg.risk.atr_stop_mult = 1.5
        cfg.risk.atr_stop_ratio_min = 0.5
        cfg.risk.atr_stop_ratio_max = 2.0
        cfg.risk.atr_stop_max_risk_mult = 4.0
        engine = EntryEngine(config=cfg)

        result = engine.calculate_stop_loss(
            "LONG",
            self.entry_zone,
            self.pip,
            entry_price=self.entry_price,
            pair="EURUSD",
            m5_df=_make_m5_df(half_range=0.0015),
        )

        structure_sl = self._structure_sl()
        structure_distance = abs(self.entry_price - structure_sl)
        expected_distance = structure_distance * cfg.risk.atr_stop_ratio_max

        assert result < structure_sl
        assert abs(self.entry_price - result) == pytest.approx(expected_distance, rel=1e-6)

    def test_mode_on_falls_back_to_structure_when_atr_unavailable(self):
        cfg = AppConfig()
        cfg.risk.volatility_stop_mode = "on"
        cfg.risk.atr_stop_period = 14
        engine = EntryEngine(config=cfg)

        result = engine.calculate_stop_loss(
            "LONG",
            self.entry_zone,
            self.pip,
            entry_price=self.entry_price,
            pair="EURUSD",
            m5_df=_make_m5_df(n=5),
        )

        assert result == self._structure_sl()
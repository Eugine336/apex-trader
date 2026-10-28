"""SystemVolatilityMonitor sizing gate (the live part of the retired quality-layer tests)."""

from brain.regime_detector import MarketRegime, RegimeAnalysis, SystemVolatilityMonitor


def _spike_analysis() -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=MarketRegime.VOLATILE, atr_current=0.002, atr_average=0.001,
        volatility_ratio=2.5, directional_strength=0.1, confidence=0.9,
        tradeable=False,
    )


def test_monitor_spike_cuts_size_multiplier():
    mon = SystemVolatilityMonitor()
    mon.update([_spike_analysis()])
    assert mon.get_size_multiplier() == SystemVolatilityMonitor.SPIKE_MULTIPLIER

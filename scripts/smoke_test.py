"""
APEX TRADER — Smoke Test
Imports all 8 phases and verifies the full system can boot without errors.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    print("Phase 1 — Brain…")
    from brain import (
        StructureEngine, LiquidityMapper, FVGDetector,
        OrderBlockDetector, CurrencyStrengthMeter, SessionEngine, NewsGuard,
        RegimeDetector, VolumeAnalyzer, InducementDetector, WyckoffEngine,
        MTFOrchestrator, TradeJournal, DrawdownGuard, ExecutionMonitor,
        CorrelationEngine, BacktestEngine, BrokerDataLoader,
    )
    print("  ✅ 18 modules loaded")

    print("Phase 2 — Scanner…")
    from scanner import PairScanner, PairRanker, ScanScheduler
    print("  ✅ 3 modules loaded")

    print("Phase 3 — Trigger…")
    from trigger import EntryEngine, EntryPatternDetector, EntryValidator
    print("  ✅ 3 modules loaded")

    print("Phase 4 — Management…")
    from management import TradeManager, PartialCloseCalculator, StructureTrailingStop, ReEntryManager
    print("  ✅ 4 modules loaded")

    print("Phase 5 — Risk…")
    from risk import RiskEngine, PositionSizer, PnLTracker, SpreadMonitor, RiskReporter
    print("  ✅ 5 modules loaded")

    print("Phase 6 — Adaptive Optimizer…")
    from adaptive import AdaptiveOptimizer, TradeAnalyzer, ScoreOptimizer, RegimeLearner, PairLearner, SessionLearner
    print("  ✅ 6 modules loaded")

    print("Phase 7 — Platforms…")
    from platforms import PlatformManager, TradingLoop
    print("  ✅ 2 modules loaded")

    print("Phase 8 — Dashboard…")
    from dashboard.api import create_app
    from dashboard.state import LiveState
    print("  ✅ 2 modules loaded")

    print("\nVerifying EntryEngine is reachable from TradingLoop…")
    from config import AppConfig
    loop = TradingLoop(AppConfig())
    assert hasattr(loop, "entry_engine"), "TradingLoop missing entry_engine"
    assert isinstance(loop.entry_engine, EntryEngine), "entry_engine is not EntryEngine"
    print("  ✅ EntryEngine wired into TradingLoop")

    print("\n" + "=" * 50)
    print("  ALL SYSTEMS NOMINAL")
    print("=" * 50)


if __name__ == "__main__":
    main()

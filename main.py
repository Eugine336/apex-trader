"""
APEX TRADER — Entry Point
The sniper is awake, systems checked, and waiting for precision setups.
"""

from datetime import datetime, timezone

from loguru import logger

from brain import (
    CurrencyStrengthMeter,
    FVGDetector,
    LiquidityMapper,
    OrderBlockDetector,
    SessionEngine,
    StructureEngine,
)
from config import get_config


def build_system_status() -> dict:
    config = get_config()
    utc_now = datetime.now(timezone.utc)
    session_status = SessionEngine().get_status(utc_now)

    return {
        "timestamp_utc": utc_now.isoformat(),
        "phase": "Phase 1 — Brain",
        "modules_loaded": [
            "StructureEngine",
            "LiquidityMapper",
            "FVGDetector",
            "OrderBlockDetector",
            "CurrencyStrengthMeter",
            "SessionEngine",
        ],
        "risk_per_trade": config.risk.risk_per_trade,
        "max_daily_drawdown": config.risk.max_daily_drawdown,
        "min_entry_score": config.scoring.min_entry_score,
        "session": session_status.current_session,
        "session_tradeable": session_status.is_tradeable,
        "ready_for_next_phases": True,
    }


def main() -> None:
    _ = StructureEngine()
    _ = LiquidityMapper()
    _ = FVGDetector()
    _ = OrderBlockDetector()
    _ = CurrencyStrengthMeter()

    status = build_system_status()

    logger.info("APEX TRADER BOOT COMPLETE")
    logger.info(f"UTC: {status['timestamp_utc']}")
    logger.info(f"Current session: {status['session']}")
    logger.info(f"Session tradeable: {status['session_tradeable']}")
    logger.info(f"Risk per trade: {status['risk_per_trade']:.2%}")
    logger.info(f"Daily drawdown limit: {status['max_daily_drawdown']:.2%}")
    logger.info(f"Minimum entry score: {status['min_entry_score']}")
    logger.info(
        "Brain modules are live. Scanner/Trigger/Execution phases are placeholders for now."
    )


if __name__ == "__main__":
    main()

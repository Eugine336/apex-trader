"""
APEX TRADER — Central Configuration
This is the command center where risk, scoring, sessions, and
execution defaults are defined before the sniper goes live.
"""

import os
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()


ALL_MAJOR_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY",
    "EURCHF", "EURAUD", "EURCAD", "EURNZD", "GBPAUD", "GBPCAD", "GBPCHF",
    "GBPNZD", "AUDCAD", "AUDCHF", "AUDNZD", "NZDCAD", "NZDCHF", "CADCHF",
]


@dataclass
class RiskSettings:
    risk_per_trade: float = 0.02
    max_daily_drawdown: float = 0.05
    max_open_trades: int = 6
    max_correlated_trades: int = 3
    max_currency_exposure: float = 0.04


@dataclass
class ScoringSettings:
    min_entry_score: int = 85
    min_watchlist_score: int = 70
    ranging_score_cap: int = 80
    volatile_trade_freeze: bool = True


@dataclass
class TimeframeSettings:
    bias_timeframe: str = "H4"
    confirmation_timeframe: str = "H1"
    entry_timeframes: list[str] = field(default_factory=lambda: ["M15", "M5", "M1"])


@dataclass
class SessionSettings:
    enabled: bool = True
    high_liquidity_sessions: list[str] = field(
        default_factory=lambda: ["LONDON", "NEW_YORK", "OVERLAP_LONDON_NY"]
    )
    dead_zones_enabled: bool = True
    require_active_session_for_entries: bool = True


@dataclass
class FVGSettings:
    min_size_pips: float = 2.0
    pip_size: float = 0.0001
    confluence_overlap_pips: float = 5.0


@dataclass
class OrderBlockSettings:
    min_impulse_pips: float = 10.0
    lookback_candles: int = 50
    pip_size: float = 0.0001


@dataclass
class LiquiditySettings:
    equal_threshold_pips: float = 3.0
    min_touches: int = 2
    pip_size: float = 0.0001


@dataclass
class RegimeSettings:
    atr_period: int = 14
    atr_average_period: int = 20
    range_ma_period: int = 20
    range_threshold_pct: float = 0.006
    strong_trend_strength: float = 0.55
    weak_trend_strength: float = 0.35
    volatile_ratio: float = 1.8


@dataclass
class NewsGuardSettings:
    pause_before_mins: int = 2
    pause_after_mins: int = 2
    blocked_impacts: list[str] = field(default_factory=lambda: ["HIGH"])


@dataclass
class DrawdownRecoverySettings:
    normal_risk: float = 0.02
    caution_risk: float = 0.015
    recovery_risk: float = 0.01
    caution_score_threshold: int = 88
    recovery_score_threshold: int = 92
    freeze_daily_loss_threshold: float = 0.05
    recovery_mode_daily_loss_threshold: float = 0.03
    ramp_back_wins: int = 2
    equity_slope_window_days: int = 5


@dataclass
class BacktestingDefaults:
    starting_balance: float = 10_000.0
    default_pair: str = "EURUSD"
    walk_forward_train_ratio: float = 0.7
    monte_carlo_iterations: int = 500
    commission_per_lot: float = 0.0
    slippage_pips: float = 0.1
    spread_pips: float = 1.2


@dataclass
class PlatformSecrets:
    mt5_login: Optional[str] = field(default_factory=lambda: os.getenv("MT5_LOGIN"))
    mt5_password: Optional[str] = field(default_factory=lambda: os.getenv("MT5_PASSWORD"))
    mt5_server: Optional[str] = field(default_factory=lambda: os.getenv("MT5_SERVER"))
    deriv_api_token: Optional[str] = field(default_factory=lambda: os.getenv("DERIV_API_TOKEN"))
    deriv_app_id: Optional[str] = field(default_factory=lambda: os.getenv("DERIV_APP_ID"))


@dataclass
class AppConfig:
    risk: RiskSettings = field(default_factory=RiskSettings)
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
    timeframes: TimeframeSettings = field(default_factory=TimeframeSettings)
    sessions: SessionSettings = field(default_factory=SessionSettings)
    pairs: list[str] = field(default_factory=lambda: list(ALL_MAJOR_PAIRS))
    fvg: FVGSettings = field(default_factory=FVGSettings)
    order_block: OrderBlockSettings = field(default_factory=OrderBlockSettings)
    liquidity: LiquiditySettings = field(default_factory=LiquiditySettings)
    regime: RegimeSettings = field(default_factory=RegimeSettings)
    news_guard: NewsGuardSettings = field(default_factory=NewsGuardSettings)
    drawdown_recovery: DrawdownRecoverySettings = field(default_factory=DrawdownRecoverySettings)
    backtesting: BacktestingDefaults = field(default_factory=BacktestingDefaults)
    secrets: PlatformSecrets = field(default_factory=PlatformSecrets)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


CONFIG = AppConfig()


def get_config() -> AppConfig:
    return CONFIG

"""
APEX TRADER — Master Configuration
All system-wide settings live here.
Override sensitive values via .env
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ============================================
# PLATFORM CREDENTIALS
# ============================================
MT5 = {
    "login": int(os.getenv("MT5_LOGIN", 0)),
    "password": os.getenv("MT5_PASSWORD", ""),
    "server": os.getenv("MT5_SERVER", ""),
}

DERIV = {
    "api_token": os.getenv("DERIV_API_TOKEN", ""),
    "app_id": os.getenv("DERIV_APP_ID", "1089"),
    "websocket_url": "wss://ws.binaryws.com/websockets/v3",
}

# ============================================
# PAIRS TO TRADE
# ============================================
FOREX_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "AUDUSD", "NZDUSD", "USDCAD",
    "EURGBP", "EURJPY", "GBPJPY",
    "AUDJPY", "CADJPY", "CHFJPY",
    "EURCHF", "EURAUD", "EURCAD",
    "GBPAUD", "GBPCAD", "GBPCHF",
    "AUDCAD", "AUDCHF", "AUDNZD",
]

DERIV_SYNTHETIC = [
    "R_10",   # Volatility 10
    "R_25",   # Volatility 25
    "R_50",   # Volatility 50
    "R_75",   # Volatility 75
    "R_100",  # Volatility 100
    "BOOM500",
    "BOOM1000",
    "CRASH500",
    "CRASH1000",
    "stpRNG", # Step Index
]

# ============================================
# TIMEFRAMES
# ============================================
TIMEFRAMES = {
    "bias":   "H4",   # Big picture direction
    "structure": "H1", # Market structure
    "entry":  "M5",   # Entry confirmation
    "trigger": "M1",  # Precise trigger
}

# ============================================
# RISK MANAGEMENT
# ============================================
RISK = {
    "risk_per_trade": float(os.getenv("RISK_PER_TRADE", 0.02)),     # 2% per trade
    "max_daily_loss": float(os.getenv("MAX_DAILY_LOSS", 0.05)),      # 5% daily stop
    "max_weekly_loss": 0.10,                                          # 10% weekly stop
    "max_open_trades": int(os.getenv("MAX_OPEN_TRADES", 6)),         # Max simultaneous
    "min_risk_reward": 1.5,                                           # Minimum R:R
    "partial_close_pct": 0.50,                                        # Close 50% at TP1
    "breakeven_trigger": 1.0,                                         # Move SL at 1:1
    "max_spread_pips": 3.0,                                           # Skip if spread too wide
    "max_correlated_pairs": 2,                                        # Max same-currency trades
}

# ============================================
# ENTRY SCORING THRESHOLDS
# ============================================
SCORING = {
    "min_score_watchlist": 65,   # Flag for watching
    "min_score_entry": 85,       # Required to enter
    "weights": {
        "htf_structure":     20,  # H4 trend aligned
        "currency_strength": 10,  # Strong vs weak
        "session_timing":    10,  # Correct session
        "liquidity_sweep":   20,  # Sweep confirmed
        "fvg_present":       15,  # FVG as entry
        "choch_confirmed":   15,  # Change of character
        "candle_pattern":    10,  # Confirmation candle
    }
}

# ============================================
# SESSION TIMES (UTC)
# ============================================
SESSIONS = {
    "tokyo":    {"open": "00:00", "close": "09:00"},
    "london":   {"open": "07:00", "close": "16:00"},
    "new_york": {"open": "12:00", "close": "21:00"},
    "overlap":  {"open": "12:00", "close": "16:00"},  # Best session
}

PREFERRED_SESSIONS = ["overlap", "london", "new_york"]

# ============================================
# NEWS FILTER
# ============================================
NEWS = {
    "pause_before_minutes": 2,   # Pause X min before high impact news
    "pause_after_minutes":  2,   # Pause X min after high impact news
    "impact_levels": ["HIGH"],   # Which impact levels to filter
    "api_key": os.getenv("NEWS_API_KEY", ""),
}

# ============================================
# TRADE MANAGEMENT
# ============================================
TRADE_MANAGEMENT = {
    "max_candles_in_trade": 50,      # Time-based exit
    "trail_activation_rr":  1.5,     # Start trailing at 1.5R
    "trail_by_structure":   True,    # Trail using swing points
    "reentry_allowed":      True,    # Re-enter if stopped at BE
    "reentry_max_attempts": 2,       # Max re-entries per setup
}

# ============================================
# ML ADAPTER
# ============================================
ML = {
    "enabled": True,
    "min_trades_to_learn": 50,       # Min trades before ML kicks in
    "retrain_every_trades": 20,      # Retrain model every N trades
    "model_path": "ml/models/",
    "feature_importance_threshold": 0.05,
}

# ============================================
# DATABASE
# ============================================
DATABASE = {
    "url": os.getenv("DATABASE_URL", "sqlite:///data/trades.db"),
    "echo": False,
}

# ============================================
# LOGGING
# ============================================
LOGGING = {
    "level": "INFO",
    "file": "logs/apex_trader.log",
    "rotation": "1 day",
    "retention": "30 days",
}

# ============================================
# DASHBOARD
# ============================================
DASHBOARD = {
    "port": int(os.getenv("DASHBOARD_PORT", 3000)),
    "host": "localhost",
}

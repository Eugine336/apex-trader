"""
APEX TRADER — Shared Dashboard State
Central in-memory store that the API and WebSocket read from.
The trading loop (or mock data) writes here; the dashboard reads.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class BotStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class RiskMode(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    RECOVERY = "RECOVERY"
    FROZEN = "FROZEN"


class TradeDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ManagementStage(str, Enum):
    MONITORING = "MONITORING"
    TP1_HIT = "TP1_HIT"
    BREAKEVEN = "BREAKEVEN"
    TRAILING = "TRAILING"
    CLOSED = "CLOSED"


@dataclass
class ActiveTrade:
    id: str
    instrument: str
    direction: str
    entry_price: float
    current_price: float
    stop_loss: float
    tp1: float
    tp2: float
    pnl_pips: float
    pnl_dollars: float
    score: int
    stage: str
    opened_at: str
    lot_size: float


@dataclass
class ClosedTrade:
    id: str
    instrument: str
    direction: str
    entry_price: float
    exit_price: float
    pnl_pips: float
    pnl_dollars: float
    score: int
    duration_minutes: float
    opened_at: str
    closed_at: str
    outcome: str


@dataclass
class InstrumentScore:
    symbol: str
    name: str
    category: str
    score: int
    direction: str
    status: str
    factors: dict


@dataclass
class DashboardState:
    bot_status: str = BotStatus.STOPPED.value
    started_at: float = 0.0
    risk_mode: str = RiskMode.NORMAL.value
    active_trades: list = field(default_factory=list)
    closed_trades: list = field(default_factory=list)
    instrument_scores: list = field(default_factory=list)
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    monthly_pnl: float = 0.0
    total_pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    account_balance: float = 10000.0
    daily_loss_pct: float = 0.0
    max_daily_loss_pct: float = 5.0
    exposure_pct: float = 0.0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    ml_score_adjustments: dict = field(default_factory=dict)
    ml_regime_stats: dict = field(default_factory=dict)
    ml_session_stats: dict = field(default_factory=dict)
    ml_pair_stats: dict = field(default_factory=dict)
    pnl_history: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)

    @property
    def uptime_seconds(self) -> float:
        if self.started_at == 0:
            return 0.0
        return time.time() - self.started_at

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        if total == 0:
            return 0.0
        return round(self.win_count / total * 100, 1)

    @property
    def total_trades(self) -> int:
        return self.win_count + self.loss_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_status": self.bot_status,
            "uptime_seconds": self.uptime_seconds,
            "risk_mode": self.risk_mode,
            "win_rate": self.win_rate,
            "total_trades": self.total_trades,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "daily_pnl": self.daily_pnl,
            "weekly_pnl": self.weekly_pnl,
            "monthly_pnl": self.monthly_pnl,
            "total_pnl": self.total_pnl,
            "account_balance": self.account_balance,
            "daily_loss_pct": self.daily_loss_pct,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "exposure_pct": self.exposure_pct,
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "open_trade_count": len(self.active_trades),
        }


STATE = DashboardState()


def load_demo_data() -> None:
    """Populate state with realistic demo data for dashboard development."""
    import random

    STATE.bot_status = BotStatus.RUNNING.value
    STATE.started_at = time.time() - 7200
    STATE.risk_mode = RiskMode.NORMAL.value
    STATE.account_balance = 10842.50
    STATE.daily_pnl = 342.50
    STATE.weekly_pnl = 1280.00
    STATE.monthly_pnl = 2842.50
    STATE.total_pnl = 2842.50
    STATE.win_count = 47
    STATE.loss_count = 11
    STATE.daily_loss_pct = 0.8
    STATE.exposure_pct = 4.2
    STATE.consecutive_losses = 0
    STATE.consecutive_wins = 3

    demo_active = [
        ActiveTrade(
            id="T001", instrument="GBPUSD", direction="LONG",
            entry_price=1.27295, current_price=1.27480,
            stop_loss=1.27250, tp1=1.27470, tp2=1.27650,
            pnl_pips=18.5, pnl_dollars=185.0, score=94,
            stage="TRAILING", opened_at="2026-05-29T08:54:00Z",
            lot_size=0.50,
        ),
        ActiveTrade(
            id="T002", instrument="XAUUSD", direction="SHORT",
            entry_price=2348.50, current_price=2345.80,
            stop_loss=2352.00, tp1=2340.00, tp2=2330.00,
            pnl_pips=27.0, pnl_dollars=270.0, score=91,
            stage="TP1_HIT", opened_at="2026-05-29T09:12:00Z",
            lot_size=0.30,
        ),
        ActiveTrade(
            id="T003", instrument="US100", direction="LONG",
            entry_price=18420.5, current_price=18415.2,
            stop_loss=18400.0, tp1=18480.0, tp2=18550.0,
            pnl_pips=-5.3, pnl_dollars=-53.0, score=87,
            stage="MONITORING", opened_at="2026-05-29T10:05:00Z",
            lot_size=0.20,
        ),
    ]
    STATE.active_trades = [t.__dict__ for t in demo_active]

    pairs = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US100", "GBPJPY",
             "AUDUSD", "V75_1S", "GER40", "NZDUSD"]
    outcomes = ["WIN", "WIN", "WIN", "WIN", "LOSS", "WIN", "WIN", "WIN",
                "LOSS", "WIN"]
    demo_closed = []
    for i, (pair, outcome) in enumerate(zip(pairs, outcomes)):
        pnl = round(random.uniform(50, 350), 2) if outcome == "WIN" else round(
            random.uniform(-200, -50), 2)
        demo_closed.append(ClosedTrade(
            id=f"H{i:03d}", instrument=pair,
            direction=random.choice(["LONG", "SHORT"]),
            entry_price=round(random.uniform(1.0, 2500.0), 5),
            exit_price=round(random.uniform(1.0, 2500.0), 5),
            pnl_pips=round(pnl / 10, 1), pnl_dollars=pnl,
            score=random.randint(85, 98),
            duration_minutes=round(random.uniform(5, 120), 1),
            opened_at=f"2026-05-2{8 - i // 3}T{8 + i}:00:00Z",
            closed_at=f"2026-05-2{8 - i // 3}T{9 + i}:00:00Z",
            outcome=outcome,
        ).__dict__)
    STATE.closed_trades = demo_closed

    from config import INSTRUMENT_REGISTRY
    scores = []
    for sym, info in INSTRUMENT_REGISTRY.items():
        sc = random.randint(10, 98)
        status = "READY" if sc >= 85 else ("WATCHLIST" if sc >= 70 else "INACTIVE")
        direction = random.choice(["LONG", "SHORT"]) if sc >= 70 else "NEUTRAL"
        scores.append(InstrumentScore(
            symbol=sym, name=info.name, category=info.category.value,
            score=sc, direction=direction, status=status,
            factors={
                "structure": random.randint(0, 20),
                "order_block": random.randint(0, 20),
                "fvg": random.randint(0, 15),
                "mtf_confluence": random.randint(0, 15),
                "session": random.randint(0, 10),
                "news": random.randint(0, 10),
                "currency_strength": random.randint(0, 10),
            },
        ).__dict__)
    STATE.instrument_scores = sorted(scores, key=lambda x: x["score"],
                                     reverse=True)

    STATE.ml_score_adjustments = {
        "structure": +2, "order_block": -1, "fvg": +3,
        "mtf_confluence": 0, "session": +1, "news": -2,
        "currency_strength": -1,
    }
    STATE.ml_regime_stats = {
        "TRENDING_STRONG": {"win_rate": 88.5, "trades": 28, "recommendation": "AGGRESSIVE"},
        "TRENDING_WEAK": {"win_rate": 76.0, "trades": 22, "recommendation": "NORMAL"},
        "RANGING": {"win_rate": 52.0, "trades": 15, "recommendation": "CAUTIOUS"},
        "VOLATILE": {"win_rate": 30.0, "trades": 6, "recommendation": "AVOID"},
    }
    STATE.ml_session_stats = {
        "LONDON_NY_OVERLAP": {"win_rate": 91.0, "trades": 18, "aggression": "HIGH"},
        "LONDON": {"win_rate": 82.0, "trades": 24, "aggression": "NORMAL"},
        "NEW_YORK": {"win_rate": 76.0, "trades": 20, "aggression": "NORMAL"},
        "TOKYO": {"win_rate": 55.0, "trades": 12, "aggression": "LOW"},
        "SYDNEY": {"win_rate": 60.0, "trades": 4, "aggression": "LOW"},
    }
    STATE.ml_pair_stats = {
        "GBPUSD": {"win_rate": 84.0, "trades": 32, "size_mult": 1.0},
        "EURUSD": {"win_rate": 79.0, "trades": 28, "size_mult": 1.0},
        "XAUUSD": {"win_rate": 71.0, "trades": 18, "size_mult": 0.8},
        "US100": {"win_rate": 65.0, "trades": 12, "size_mult": 0.8},
        "NZDJPY": {"win_rate": 38.0, "trades": 8, "size_mult": 0.0},
    }

    base = STATE.account_balance - STATE.total_pnl
    curve = [base]
    for _ in range(30):
        change = random.uniform(-100, 200)
        curve.append(round(curve[-1] + change, 2))
    STATE.equity_curve = [
        {"date": f"2026-05-{max(1, i):02d}", "equity": v}
        for i, v in enumerate(curve)
    ]
    STATE.pnl_history = [
        {"date": f"2026-05-{max(1, i):02d}",
         "pnl": round(random.uniform(-150, 400), 2)}
        for i in range(30)
    ]

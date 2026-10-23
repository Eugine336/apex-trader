"""Gold-specialist opportunity research layer for Apex Trader.

This package is deliberately broker-agnostic. It discovers and ranks XAUUSD
opportunities; the existing risk and execution stack remains the only layer
allowed to authorize orders.
"""
from gold_desk.coordinator import CoordinatorConfig, EvaluationResult, StrategyCoordinator
from gold_desk.models import Candle, Direction, EventWindow, MarketSnapshot, TradeIdea, Venue
from gold_desk.risk import GoldExecutionGate, RiskState, GateDecision
from gold_desk.strategies import default_strategies

__all__ = [
    "Candle", "Direction", "EventWindow", "MarketSnapshot", "TradeIdea", "Venue",
    "CoordinatorConfig", "EvaluationResult", "StrategyCoordinator",
    "GoldExecutionGate", "RiskState", "GateDecision", "default_strategies",
]

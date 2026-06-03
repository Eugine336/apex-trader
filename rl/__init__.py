"""
APEX RL Package
================
Emergent intelligence subsystem for APEX Trader.

Modules:
    environment  — RL trading environment (simulation)
    network      — Neural architecture (encoder + policy + value)
    trainer      — PPO training loop
    shadow       — Shadow trading / reality validation
    authority    — 7-stage authority progression manager
    bridge       — Drop-in integration with existing APEX scanner
    obs_builder  — Converts APEX market data to RL observations
"""

from .environment import ApexTradingEnv
from .network     import ApexRLAgent, ACTION_LABELS
from .trainer     import PPOTrainer, PPOConfig
from .shadow      import ShadowEngine, RLSignal
from .authority   import AuthorityManager, Permissions, STAGES
from .bridge      import RLBridge, AugmentedScore

__all__ = [
    "ApexTradingEnv",
    "ApexRLAgent",
    "ACTION_LABELS",
    "PPOTrainer",
    "PPOConfig",
    "ShadowEngine",
    "RLSignal",
    "AuthorityManager",
    "Permissions",
    "STAGES",
    "RLBridge",
    "AugmentedScore",
]

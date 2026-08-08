"""
APEX RL Package
================
Emergent intelligence subsystem for APEX Trader.

Modules:
    environment      — Single-TF RL trading environment (legacy)
    mtf_environment  — Multi-TF environment with reality-faithful execution
    network          — Neural architecture (encoder + policy + value + optional context)
    trainer          — PPO training loop
    shadow           — Shadow trading / reality validation
    authority        — 7-stage authority progression manager
    bridge           — Drop-in integration with existing APEX scanner
    obs_builder      — Single-TF observation builder
    multi_tf_obs_builder — Multi-TF observation builder (4 TFs × 12 features → (50,48))
    contracts        — Observation schema contract + checkpoint gating
"""

from .environment import ApexTradingEnv
from .mtf_environment import ApexMultiTFTradingEnv
from .network     import ApexRLAgent, ACTION_LABELS
from .trainer     import PPOTrainer, PPOConfig
from .shadow      import ShadowEngine, RLSignal
from .authority   import AuthorityManager, Permissions, STAGES
from .bridge      import RLBridge, AugmentedScore
from .contracts   import OBS_CONTRACT_VERSION, OBS_SHAPE, N_CONTEXT_FEATURES
from .multi_tf_obs_builder import MultiTFObservationBuilder

__all__ = [
    "ApexTradingEnv",
    "ApexMultiTFTradingEnv",
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
    "OBS_CONTRACT_VERSION",
    "OBS_SHAPE",
    "N_CONTEXT_FEATURES",
    "MultiTFObservationBuilder",
]

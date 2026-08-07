"""
APEX TRADER — Adaptive Optimizer Package
Statistical heuristic optimization — learns from every trade,
adapts weights, profiles pairs, sessions, and regimes.
"""

from adaptive.trade_analyzer import PerformanceProfile, TradeAnalyzer
from adaptive.score_optimizer import ScoreOptimizer, ScoringWeights
from adaptive.regime_learner import RegimeLearner, RegimeStrategy
from adaptive.pair_learner import PairLearner, PairProfile
from adaptive.session_learner import SessionLearner, SessionProfile
from adaptive.optimizer import AdaptiveOptimizer, MLAdapter, OptimizationReport, TradeAdjustments
from adaptive.ev_estimator import EVEstimator, EVEstimate
from adaptive.signal_ledger import SignalLedger, SignalRecord, SignalOutcome
from adaptive.emitter_feedback import (
    EmitterFeedbackService,
    EmitterFeedbackRequest,
    EmitterFeedbackResponse,
)
from adaptive.vote_calibrator import (
    VoteCalibrator,
    VoteCalibration,
    DEFAULT_VOTE_MODULES,
)
from adaptive.capital_allocator import (
    CapitalAllocator,
    FingerprintStats,
    compute_fingerprint,
)
from adaptive.tunable import Tunable, TuneContext, TuneResult, TuneFrequency
from adaptive.tuner_agent import TunerAgent
from adaptive.tunable_adapters import (
    ScoreOptimizerTunable,
    RegimeLearnerTunable,
    PairLearnerTunable,
    SessionLearnerTunable,
    EVEstimatorTunable,
    GateTunerTunable,
    PlannerCalibratorTunable,
    SignalLedgerTunable,
    VoteCalibratorTunable,
    CapitalAllocatorTunable,
)

__all__ = [
    "TradeAnalyzer", "PerformanceProfile",
    "ScoreOptimizer", "ScoringWeights",
    "RegimeLearner", "RegimeStrategy",
    "PairLearner", "PairProfile",
    "SessionLearner", "SessionProfile",
    "AdaptiveOptimizer", "MLAdapter", "OptimizationReport", "TradeAdjustments",
    "EVEstimator", "EVEstimate",
    "SignalLedger", "SignalRecord", "SignalOutcome",
    "EmitterFeedbackService", "EmitterFeedbackRequest", "EmitterFeedbackResponse",
    "VoteCalibrator", "VoteCalibration", "DEFAULT_VOTE_MODULES",
    "CapitalAllocator", "FingerprintStats", "compute_fingerprint",
    "Tunable", "TuneContext", "TuneResult", "TuneFrequency",
    "TunerAgent",
    "ScoreOptimizerTunable", "RegimeLearnerTunable", "PairLearnerTunable",
    "SessionLearnerTunable", "EVEstimatorTunable", "GateTunerTunable",
    "PlannerCalibratorTunable", "SignalLedgerTunable",
    "VoteCalibratorTunable",
    "CapitalAllocatorTunable",
]

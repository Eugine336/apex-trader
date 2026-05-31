"""Backward-compatibility shim — real implementation in adaptive.session_learner."""
from adaptive.session_learner import SessionLearner, SessionProfile  # noqa: F401

__all__ = ["SessionLearner", "SessionProfile"]

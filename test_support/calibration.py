"""Isolated test-only calibration artifact issuer."""
from __future__ import annotations

import hashlib
import uuid
import time

from cognition.calibration_artifacts import CalibrationArtifact, _register_artifact


def register_test_calibration(
    symbol: str,
    direction: str = "LONG",
    *,
    horizon_seconds: int = 300,
    probability: float = 0.72,
    sample_count: int = 240,
    brier_score: float = 0.17,
    reliability_gap: float = 0.08,
    expected_return_r: float = 0.25,
    holdout_expected_return_r: float = 0.25,
    expected_return_gap: float = 0.0,
    selection_coverage: float = 1.0,
    ttl_seconds: float = 3600.0,
) -> CalibrationArtifact:
    now = time.time()
    artifact_id = "test-" + uuid.uuid4().hex
    artifact = CalibrationArtifact(
        artifact_id=artifact_id,
        symbol=str(symbol).strip().upper(),
        direction=str(direction).strip().upper(),
        horizon_seconds=int(horizon_seconds),
        win_probability=float(probability),
        expected_return_r=float(expected_return_r),
        holdout_expected_return_r=float(holdout_expected_return_r),
        expected_return_gap=float(expected_return_gap),
        sample_count=int(sample_count),
        brier_score=float(brier_score),
        reliability_gap=float(reliability_gap),
        dataset_digest=hashlib.sha256(artifact_id.encode("utf-8")).hexdigest(),
        issued_at_epoch=now,
        expires_at_epoch=now + float(ttl_seconds),
        walk_forward_folds=3,
        selection_coverage=float(selection_coverage),
    )
    _register_artifact(artifact)
    return artifact

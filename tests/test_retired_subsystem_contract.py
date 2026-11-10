"""Regression contract for the retired RL subsystem and retained native learning."""

from dataclasses import fields
from pathlib import Path

from core.system_context import SystemContext
from platforms.health_watchdog import HealthReport as WatchdogHealthReport


ROOT = Path(__file__).resolve().parents[1]


def test_rl_package_and_training_entrypoints_are_absent():
    for relative in (
        "rl",
        "run_training.py",
        "train_colab.py",
        "scripts/init_rl_checkpoint.py",
        "scripts/validate_rl_data.py",
        "scripts/verify_rl_data.py",
    ):
        assert not (ROOT / relative).exists(), relative


def test_runtime_contracts_have_no_rl_fields_or_bridge():
    assert "rl_bridge" not in SystemContext.__dataclass_fields__
    assert "rl_enabled" not in {item.name for item in fields(WatchdogHealthReport)}
    assert "rl_stage" not in {item.name for item in fields(WatchdogHealthReport)}


def test_native_and_statistical_learning_surfaces_remain():
    fields_present = SystemContext.__dataclass_fields__
    assert "cognitive_brain" in fields_present
    assert "brain_calibration" in fields_present
    assert "ml_adapter" in fields_present
    assert "campaign_memory" in fields_present


def test_pytorch_is_not_a_runtime_or_ci_dependency():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8").lower()
    assert "torch" not in requirements
    assert "pip install torch" not in ci

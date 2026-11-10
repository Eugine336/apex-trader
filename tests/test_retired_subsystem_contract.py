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



def test_planning_subsystem_is_physically_retired():
    """No production module may import or reconstruct the deleted planner stack."""
    import re

    for relative in (
        "planning",
        "planning/__init__.py",
        "planning/trade_planner.py",
        "planning/calibrator.py",
        "planning/outcome_logger.py",
        "planning/models.py",
    ):
        assert not (ROOT / relative).exists(), relative

    import_pattern = re.compile(
        r"^\s*(?:from\s+planning(?:\.|\s)|import\s+planning(?:\.|,|\s|$))",
        re.MULTILINE,
    )
    for source in ROOT.rglob("*.py"):
        parts = set(source.parts)
        if parts.intersection({".git", ".venv", "venv", "__pycache__", "tests"}):
            continue
        content = source.read_text(encoding="utf-8", errors="replace")
        assert not import_pattern.search(content), f"retired planning import in {source.relative_to(ROOT)}"

    config_source = (ROOT / "config.py").read_text(encoding="utf-8")
    context_source = (ROOT / "core/system_context.py").read_text(encoding="utf-8")
    adapters_source = (ROOT / "adaptive/tunable_adapters.py").read_text(encoding="utf-8")
    tuner_source = (ROOT / "adaptive/tuner_agent.py").read_text(encoding="utf-8")
    assert "PlannerConfig" not in config_source
    assert "trade_planner" not in context_source
    assert "outcome_logger" not in context_source
    assert "calibrator" not in context_source
    assert "PlannerCalibratorTunable" not in adapters_source
    assert "planner_calibrator" not in tuner_source


def test_pytorch_is_not_a_runtime_or_ci_dependency():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8").lower()
    assert "torch" not in requirements
    assert "pip install torch" not in ci

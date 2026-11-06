"""The concept coverage register stays honest and in sync with the registered modules."""

import subprocess
import sys
from pathlib import Path

from cognition.analytical_knowledge import _ROWS
from cognition.concepts import CONCEPT_MODULES
from cognition.strategy_knowledge import STRATEGY_LENSES

ROOT = Path(__file__).resolve().parents[1]


def test_modules_only_claim_catalogued_concepts():
    methods = {r[0] for r in _ROWS}
    lenses = {l.key for l in STRATEGY_LENSES}
    for module in CONCEPT_MODULES:
        assert set(module.covers_methods) <= methods, module.key
        assert set(module.covers_lenses) <= lenses, module.key


def test_data_gates_are_explicit_and_never_claim_full_coverage():
    full = {key for module in CONCEPT_MODULES for key in module.covers_methods}
    gated = {
        key: reason
        for module in CONCEPT_MODULES
        for key, reason in (getattr(module, "data_gated_methods", {}) or {}).items()
    }
    assert not (full & set(gated))
    assert all(isinstance(reason, str) and reason.strip() for reason in gated.values())


def test_every_concept_module_has_a_unique_key():
    keys = [m.key for m in CONCEPT_MODULES]
    assert len(keys) == len(set(keys))


def test_register_is_up_to_date():
    result = subprocess.run([sys.executable, "scripts/concept_coverage.py", "--check"],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

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


def test_all_79_method_keys_and_38_lenses_are_audited_against_the_live_brain_path():
    import inspect

    from cognition.brain import CognitiveBrain
    from cognition.brain_reasoning import BrainReasoner
    from cognition.concepts import read_concept_readings
    from cognition.market_lenses import analyze_market_frames
    from cognition.strategy_knowledge import assess_strategy_lenses

    # Keep the requested audit universe explicit: every catalogued method and
    # lens must be classified, not silently dropped from the generated register.
    assert len(_ROWS) == 79
    assert len(STRATEGY_LENSES) == 38
    method_keys = {row[0] for row in _ROWS}
    lens_keys = {lens.key for lens in STRATEGY_LENSES}

    full_methods = {
        key for module in CONCEPT_MODULES for key in module.covers_methods
    }
    gated_methods = {
        key for module in CONCEPT_MODULES
        for key in (getattr(module, "data_gated_methods", {}) or {})
    }
    full_lenses = {
        key for module in CONCEPT_MODULES for key in module.covers_lenses
    }
    gated_lenses = {
        key for module in CONCEPT_MODULES
        for key in (getattr(module, "data_gated_lenses", {}) or {})
    }
    assert full_methods | gated_methods <= method_keys
    assert full_lenses | gated_lenses <= lens_keys
    assert not (full_methods & gated_methods)
    assert not (full_lenses & gated_lenses)

    # Shared reader path: every registered reader is invoked, converted to
    # Evidence, admitted to market-lens output, projected by BrainReasoner, and
    # synthesized into hypotheses by the CognitiveBrain's native reasoner.
    reader_source = inspect.getsource(read_concept_readings)
    market_source = inspect.getsource(analyze_market_frames)
    analysis_source = inspect.getsource(BrainReasoner.analyze_evidence)
    hypothesis_source = inspect.getsource(BrainReasoner.generate_hypotheses)
    cognitive_analysis_source = inspect.getsource(CognitiveBrain._analyze_evidence)
    cognitive_hypothesis_source = inspect.getsource(CognitiveBrain._generate_hypotheses)
    assert "for module in registered" in reader_source
    assert "contextual_reader(symbol, bars_by_tf, module_context)" in reader_source
    assert "module.read(symbol, bars_by_tf)" in reader_source
    assert "run_concepts(" in market_source
    assert 'm.get("source_kind") == "concept_reading"' in analysis_source
    assert "concept_readings.append(" in analysis_source
    assert "synthesize_concept_readings(" in hypothesis_source
    assert "return self._native.analyze_evidence(" in cognitive_analysis_source
    assert "return self._native.generate_hypotheses(" in cognitive_hypothesis_source

    # Lens path: all registered lenses are assessed; relevant and watch states
    # are passed into Brain hypotheses. Not-evaluable lenses remain unknown and
    # are not promoted into trade hypotheses.
    assessment_source = inspect.getsource(assess_strategy_lenses)
    assert "for lens in STRATEGY_LENSES" in assessment_source
    assert "for assessment in analysis.strategy_lenses" in hypothesis_source
    assert 'assessment.get("status") not in {"relevant", "watch"}' in hypothesis_source
    assessments = assess_strategy_lenses([], [], {})
    assert {item["key"] for item in assessments} == lens_keys

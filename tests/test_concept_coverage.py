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
    from cognition.loop import EvidenceConsolidator
    from cognition.market_lenses import analyze_market_frames
    from cognition.strategy_knowledge import assess_strategy_lenses
    from core.system_context import SystemContext
    from event_driven_bootstrap import EventDrivenSystem
    from execution.action_executor import ActionExecutor
    from execution.lifecycle_loops import FlushLoop
    from execution.risk_gate import RiskGate
    from config import AppConfig

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
    system_context_source = inspect.getsource(SystemContext.create)
    consolidator_source = inspect.getsource(EvidenceConsolidator.build)
    native_reasoning_source = inspect.getsource(CognitiveBrain._native_reasoning)
    cognitive_reason_source = inspect.getsource(CognitiveBrain.reason)
    origination_sink_source = inspect.getsource(EventDrivenSystem._make_origination_sink)
    portfolio_sizing_source = inspect.getsource(
        EventDrivenSystem._size_brain_origination_with_portfolio
    )
    flush_source = inspect.getsource(FlushLoop._loop)
    executor_execute_source = inspect.getsource(ActionExecutor.execute)
    executor_dispatch_source = inspect.getsource(ActionExecutor._dispatch)
    risk_gate_source = inspect.getsource(RiskGate.validate)
    system_context_create_source = inspect.getsource(SystemContext.create)
    owner_sync_source = inspect.getsource(AppConfig.sync_owner_limit_views)
    assert "for module in registered" in reader_source
    assert "contextual_reader(symbol, bars_by_tf, module_context)" in reader_source
    assert "module.read(symbol, bars_by_tf)" in reader_source
    assert "run_concepts(" in market_source
    # Verify the live SystemContext -> EvidenceConsolidator -> MarketState route,
    # not only the isolated reader and converter functions.
    assert "analyze_market_frames(" in system_context_source
    assert "concept_source=_concept_market_source" in system_context_source
    assert "self._concept_source(ms.symbol)" in consolidator_source
    assert "for item in concept_evidence" in consolidator_source
    assert "ms.add(item)" in consolidator_source
    assert 'm.get("source_kind") == "concept_reading"' in analysis_source
    assert "concept_readings.append(" in analysis_source
    assert "assess_strategy_lenses(" in analysis_source
    assert "synthesize_concept_readings(" in hypothesis_source
    # Verify the production CognitiveBrain decision method invokes the native
    # analysis/hypothesis chain that consumes the MarketState.
    assert "self._native.analyze_evidence(" in native_reasoning_source
    assert "self._native.generate_hypotheses(" in native_reasoning_source
    assert "_native_reasoning(" in cognitive_reason_source
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

    # WP-B7: prove the one live Brain origination path passes compliance and
    # canonical portfolio sizing before the shared executor and broker gateway.
    assert "_compliance_permits(" in origination_sink_source
    assert "_size_brain_origination_with_portfolio(" in origination_sink_source
    assert "self._aggregator.submit([Intent.open(" in origination_sink_source
    assert "division.evaluate(" in portfolio_sizing_source
    assert "account_risk.daily_loss_halted(" in portfolio_sizing_source
    assert "account_risk.margin_entry_blocked(" in portfolio_sizing_source
    assert "get_all_open_positions()" in portfolio_sizing_source
    assert "owner_limits = getattr(config, \"owner_limits\", None)" in system_context_create_source
    assert "AccountRiskManager(" in system_context_create_source
    assert "sizer = getattr(ctx.risk_engine, \"position_sizer\", None)" in system_context_create_source
    assert "PortfolioDivision(" in system_context_create_source
    assert "position_sizer=sizer" in system_context_create_source
    assert "self._executor.execute_batch(" in flush_source
    assert "self._gate.validate(" in executor_execute_source
    assert "self._broker.execute_entry(" in executor_dispatch_source
    assert "return self._validate_open(intent, account_drawdown_pct)" in risk_gate_source
    # OwnerLimits are the canonical source and every legacy consumer is updated
    # from the same validated object; no independently looser duplicate remains.
    assert "limits = self.owner_limits" in owner_sync_source
    assert "risk.max_risk_pct_per_trade = limits.max_risk_pct_per_trade" in owner_sync_source
    assert "governor.max_total_risk_pct = limits.max_total_risk_pct" in owner_sync_source
    assert "risk_management.daily_drawdown_limit_pct = limits.daily_loss_limit_pct" in owner_sync_source

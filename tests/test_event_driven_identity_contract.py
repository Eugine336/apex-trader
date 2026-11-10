"""Static admission-contract checks for exposure-increasing runtime routes.

These tests intentionally use the standard library only, so they run in the
focused cognition workflow without importing the full broker bootstrap. They
protect the architectural boundary, not market profitability.
"""

import ast
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _parse(relative_path: str) -> ast.Module:
    return ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))


def _function(node: ast.AST, name: str) -> ast.FunctionDef:
    found = [
        item for item in ast.walk(node)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    ]
    assert len(found) == 1, f"expected one {name} definition, found {len(found)}"
    return found[0]


def _calls(node: ast.AST, *, attribute: str | None = None, function: str | None = None) -> list[ast.Call]:
    matches = []
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        if attribute and isinstance(item.func, ast.Attribute) and item.func.attr == attribute:
            matches.append(item)
        if function and isinstance(item.func, ast.Name) and item.func.id == function:
            matches.append(item)
    return matches


def _kind_branch(sink: ast.FunctionDef, kind: str) -> ast.If:
    for item in ast.walk(sink):
        if not isinstance(item, ast.If) or not isinstance(item.test, ast.Compare):
            continue
        test = item.test
        if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
            continue
        if not isinstance(test.left, ast.Name) or test.left.id != "kind":
            continue
        if len(test.comparators) == 1 and isinstance(test.comparators[0], ast.Constant):
            if test.comparators[0].value == kind:
                return item
    raise AssertionError(f"management sink has no explicit kind == {kind!r} branch")


def test_management_reverse_can_close_but_cannot_open_directly():
    module = _parse("event_driven_bootstrap.py")
    management_factory = _function(module, "_make_management_sink")
    sink = _function(management_factory, "_sink")
    reverse = _kind_branch(sink, "reverse")
    # Reverse is a management/close decision. The other side must be opened only
    # after a fresh Brain opportunity and whole-book admission pass.
    assert not _calls(reverse, attribute="open")
    assert _calls(reverse, attribute="close")
    assert _calls(reverse, attribute="warning") or _calls(reverse, attribute="info")


def test_management_scale_in_cannot_submit_exposure_without_entry_admission():
    module = _parse("event_driven_bootstrap.py")
    management_factory = _function(module, "_make_management_sink")
    sink = _function(management_factory, "_sink")
    scale = _kind_branch(sink, "scale_in")
    assert not _calls(scale, attribute="open")
    assert _calls(scale, attribute="warning") or _calls(scale, attribute="info")


def test_execution_sink_cannot_disable_net_expected_value_qualification():
    module = _parse("event_driven_bootstrap.py")
    method = _function(module, "_opportunity_qualifies")
    # Disabling the historical config flag must not create a return-True bypass.
    true_returns = [
        item for item in ast.walk(method)
        if isinstance(item, ast.Return)
        and isinstance(item.value, ast.Constant)
        and item.value.value is True
    ]
    assert not true_returns
    assert _calls(method, function="qualify_net_ev")
    assert _calls(method, attribute="warning")
    assert not any(
        isinstance(item, ast.Name) and item.id == "confidence"
        for item in ast.walk(method)
    ), "reasoning confidence must not enter execution-side EV qualification"
    assert any(
        keyword.arg == "expected_return_r"
        for call in _calls(method, function="qualify_net_ev")
        for keyword in call.keywords
    ), "execution-side EV must consume the verified fixed-horizon return"
    stake_method = _function(module, "_opportunity_qualifies_stake")
    assert not any(
        isinstance(item, ast.Name) and item.id == "confidence"
        for item in ast.walk(stake_method)
    ), "stake qualification must not consume reasoning confidence"


def test_legacy_atomic_reversal_runtime_wiring_is_disabled():
    module = _parse("core/system_context.py")
    calls = [
        call for call in _calls(module, function="_ReversalManager")
        if any(keyword.arg == "enabled" for keyword in call.keywords)
    ]
    assert calls, "expected an explicit reversal-manager runtime wiring site"
    assert any(
        isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        for call in calls for keyword in call.keywords if keyword.arg == "enabled"
    ), "the legacy reversal manager must not open the opposite side itself"


def test_pending_order_staging_routes_are_retired_and_brain_gateway_is_canonical():
    """Only the shared PlatformManager may invoke the MT5 pending adapter.

    Legacy news/zone staging remains deleted. The new LIMIT/STOP lifecycle is
    admitted by the Brain and ActionExecutor, then submitted by PlatformManager
    to the MT5 connector; no scheduler or alternate planner may stage orders.
    """
    module = _parse("event_driven_bootstrap.py")
    defined = {n.name for n in ast.walk(module) if isinstance(n, ast.FunctionDef)}
    for retired in (
        "_run_news_planner_update", "_stage_place_pending", "_stage_cancel_pending",
        "_stage_size", "_news_size", "_news_order_filled",
    ):
        assert retired not in defined, f"{retired} must stay deleted"
    assert not (ROOT / "brain" / "news_planner.py").exists()
    allowed_pending_calls = {Path("platforms/platform_manager.py")}
    excluded_dirs = {
        ".git", ".venv", "venv", "__pycache__", "node_modules",
        "build", "dist", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        "tests", "backtest", "test_support",
    }
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if any(part in excluded_dirs for part in rel.parts[:-1]):
            continue
        # Respect PEP 263 source-encoding declarations (for example, Big5)
        # rather than assuming every Python source file is UTF-8.
        with tokenize.open(path) as source_file:
            tree = ast.parse(source_file.read(), filename=str(rel))
        if _calls(tree, attribute="place_pending_order"):
            assert rel in allowed_pending_calls, f"{rel} bypasses the canonical pending-order gateway"
        assert not _calls(tree, attribute="place_pending_entry"), f"{rel} stages pending orders outside the shared executor"


def test_brain_identity_and_provenance_reach_execution_intent():
    module = _parse("event_driven_bootstrap.py")
    factory = _function(module, "_make_origination_sink")
    sink = _function(factory, "_sink")
    calls = _calls(sink, attribute="open")
    assert len(calls) == 1, "Brain origination must submit exactly one shared OPEN intent"
    kwargs = {item.arg: item.value for item in calls[0].keywords if item.arg}
    for name in ("decision_id", "campaign_id", "opportunity_id", "identity_version", "identity"):
        assert name in kwargs, f"origination provenance missing from execution Intent.open: {name}"


def test_brain_management_identity_and_provenance_reach_execution_intents():
    module = _parse("event_driven_bootstrap.py")
    factory = _function(module, "_make_management_sink")
    sink = _function(factory, "_sink")
    required = {"decision_id", "campaign_id", "opportunity_id", "action_id", "identity_version", "identity"}
    for method in ("close", "partial_close", "modify_sl"):
        calls = _calls(sink, attribute=method)
        assert calls, f"expected a management {method} intent site"
        for call in calls:
            kwargs = {item.arg: item.value for item in call.keywords if item.arg}
            assert required <= kwargs.keys(), f"{method} intent lost Brain provenance: {required - kwargs.keys()}"


def test_brain_management_resolves_exact_confirmed_ticket_not_first_symbol_side_match():
    module = _parse("event_driven_bootstrap.py")
    factory = _function(module, "_make_management_sink")
    resolver = _function(factory, "_open_position_for")
    resolver_source = ast.unparse(resolver)
    assert "get_open_positions_snapshot" in resolver_source
    assert "confirmed_platforms" in resolver_source
    assert "failed_platforms" in resolver_source
    assert any(isinstance(node, ast.Constant) and node.value == "ticket" for node in ast.walk(resolver))
    assert any(isinstance(node, ast.Constant) and node.value == "platform" for node in ast.walk(resolver))
    assert "get_all_open_positions" not in resolver_source
    assert "len(matches) != 1" in resolver_source


def test_position_management_context_excludes_reasoning_confidence():
    brain_module = _parse("cognition/brain.py")
    position_view = next(
        node for node in brain_module.body
        if isinstance(node, ast.ClassDef) and node.name == "PositionView"
    )
    position_fields = {
        item.target.id
        for item in position_view.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    }
    assert "entry_confidence" not in position_fields

    payload = _function(brain_module, "_management_evidence_payload")
    assert not any(
        isinstance(node, ast.Constant) and node.value == "entry_confidence"
        for node in ast.walk(payload)
    ), "the Brain must reassess positions from current evidence, not entry confidence"

    context_module = _parse("core/system_context.py")
    position_builders = [
        node for node in ast.walk(context_module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_PositionView"
    ]
    assert position_builders
    assert not any(
        keyword.arg == "entry_confidence"
        for call in position_builders for keyword in call.keywords
    )




def test_book_opportunity_comparison_never_uses_reasoning_confidence_as_ev():
    module = _parse("cognition/brain_reasoning.py")
    method = _function(module, "compare_opportunities")
    assert not any(
        isinstance(node, ast.Name) and node.id == "confidence"
        for node in ast.walk(method)
    )
    assert not any(
        isinstance(node, ast.Constant) and node.value == "entry_confidence"
        for node in ast.walk(method)
    ), "held-position confidence is not comparable expected-return evidence"




def test_breakeven_reentry_subsystem_is_fully_retired():
    assert not (ROOT / "management" / "re_entry.py").exists()

    bootstrap = _parse("event_driven_bootstrap.py")
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"check_re_entry", "_arm_re_entry_zone"}
        for node in ast.walk(bootstrap)
    )
    assert not any(
        isinstance(node, ast.Name) and node.id in {
            "ReEntryManager", "re_entry_manager", "RE_ENTRY_ARMED",
        }
        for path in ("core/system_context.py", "event_driven_bootstrap.py", "persistence/domain_events.py")
        for node in ast.walk(_parse(path))
    )




def test_disconnected_cross_instrument_heuristics_are_retired():
    for relative in (
        "brain/cross_instrument_ranker.py",
        "brain/opportunity_sizer.py",
        "management/position_displacer.py",
    ):
        assert not (ROOT / relative).exists()

    config_module = _parse("config.py")
    cross_config = next(
        node for node in config_module.body
        if isinstance(node, ast.ClassDef) and node.name == "CrossInstrumentConfig"
    )
    fields = {
        item.target.id
        for item in cross_config.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    }
    assert not fields.intersection({
        "queue_enabled", "queue_window_ms",
        "ranking_enabled", "quality_sizing_enabled", "displacement_enabled",
        "spread_ev_penalty_per_pip", "winrate_ev_weight",
        "quality_sizing_max_boost", "quality_sizing_min_cut", "quality_sizing_ev_ref",
        "displacement_ev_margin", "displacement_max_per_cycle",
        "displacement_min_profit_protect", "displacement_cooldown_seconds",
    })

    context = _parse("core/system_context.py")
    assert not any(
        isinstance(node, ast.Name) and node.id in {
            "CrossInstrumentRanker", "OpportunityQualitySizer", "PositionDisplacer",
        }
        for node in ast.walk(context)
    )
    bootstrap = _parse("event_driven_bootstrap.py")
    assert not any(
        isinstance(node, ast.Name) and node.id == "_opportunity_queue"
        for node in ast.walk(bootstrap)
    )



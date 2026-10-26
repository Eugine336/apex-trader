"""Static admission-contract checks for exposure-increasing runtime routes.

These tests intentionally use the standard library only, so they run in the
focused cognition workflow without importing the full broker bootstrap. They
protect the architectural boundary, not market profitability.
"""

import ast
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


def test_legacy_news_planner_cannot_stage_pending_orders_outside_brain_admission():
    module = _parse("event_driven_bootstrap.py")
    update = _function(module, "_run_news_planner_update")
    # The old planner's callback calls connector.place_pending_order directly,
    # without a first-class Brain opportunity or whole-book admission. The
    # scheduled update entrypoint therefore must not invoke it under any config.
    assert not _calls(update, attribute="update")
    assert not _calls(update, attribute="place_pending_order")
    assert _calls(update, attribute="warning")


def test_legacy_pending_order_callback_cannot_reach_broker_connector():
    module = _parse("event_driven_bootstrap.py")
    stage = _function(module, "_stage_place_pending")
    # Symbol allowlists are not a substitute for first-class opportunity and
    # portfolio admission; this boundary must never place broker pending orders.
    assert not _calls(stage, attribute="place_pending_order")
    assert _calls(stage, attribute="warning")
    returns = [node for node in ast.walk(stage) if isinstance(node, ast.Return)]
    assert returns
    assert all(
        isinstance(node.value, ast.Constant) and node.value.value is None
        for node in returns
    )


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
    required = {"decision_id", "campaign_id", "opportunity_id", "identity_version", "identity"}
    for method in ("close", "partial_close", "modify_sl"):
        calls = _calls(sink, attribute=method)
        assert calls, f"expected a management {method} intent site"
        for call in calls:
            kwargs = {item.arg: item.value for item in call.keywords if item.arg}
            assert required <= kwargs.keys(), f"{method} intent lost Brain provenance: {required - kwargs.keys()}"

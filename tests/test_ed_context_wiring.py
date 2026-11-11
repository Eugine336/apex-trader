"""The event-driven worker receives only non-directional safety context."""

from pathlib import Path
import ast

from execution.position_worker import MarketContext, PositionWorker


ROOT = Path(__file__).resolve().parents[1]


def test_position_evaluator_does_not_build_scan_direction_or_score_context():
    source = (ROOT / "event_driven_bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "ScanContext" not in source
    assert "_build_scan_context" not in source
    assert "scan_ctx" not in source
    assert "score_history.append(current_score)" not in source

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "evaluate"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_worker"
    ]
    assert calls
    assert all(
        not any(keyword.arg == "scan" for keyword in call.keywords)
        for call in calls
    )


def test_market_context_contains_only_non_directional_spread_facts():
    fields = set(MarketContext.__dataclass_fields__)
    assert fields == {"typical_spread", "current_spread"}


def test_worker_does_not_expose_legacy_management_methods():
    forbidden = {
        "_check_tp1", "_check_breakeven", "_check_tp2", "_check_tp3",
        "_check_stall", "_check_invalidation", "_check_conviction_collapse",
        "_check_htf_candle_close", "_check_opportunity_cost",
        "_check_structure_trailing", "_check_atr_trailing",
    }
    assert forbidden.isdisjoint(dir(PositionWorker))

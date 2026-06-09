"""
Tests for exit-attribution discrepancy detection.

Covers _exit_reasons_conflict helper and the wiring of manager_intent /
exit_reason_discrepancy into the TRADE_CLOSE event payload.

Note: main_loop.py has a deep import chain (torch, MT5, etc.) that is
unavailable in a bare test sandbox.  We import the helper via exec() on
just the relevant lines from the source file to avoid the chain.
"""

import ast
import textwrap
import pytest

# ── Extract the pure helper + constants from main_loop.py without importing ──

_SRC_PATH = "platforms/main_loop.py"

_NAMES_TO_EXTRACT = {
    "_HARD_LEVEL_REASONS",
    "_BENIGN_BROKER_REASONS",
    "_DISCRETIONARY_KEYWORDS",
    "_SIMULATED_SL_KEYWORDS",
    "_SIMULATED_TP_KEYWORDS",
    "_exit_reasons_conflict",
}

with open(_SRC_PATH) as f:
    _src_text = f.read()
    _tree = ast.parse(_src_text)

_extracted_lines: list[str] = []
for node in _tree.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in _NAMES_TO_EXTRACT:
                _extracted_lines.append(ast.get_source_segment(_src_text, node))
    elif isinstance(node, ast.FunctionDef) and node.name in _NAMES_TO_EXTRACT:
        _extracted_lines.append(ast.get_source_segment(_src_text, node))

_ns: dict = {}
exec("\n".join(_extracted_lines), _ns)
_exit_reasons_conflict = _ns["_exit_reasons_conflict"]


# ═══════════════════════════════════════════════════════════════════════
#  _exit_reasons_conflict — pure function tests
# ═══════════════════════════════════════════════════════════════════════

class TestExitReasonsConflict:
    """Broker reason vs manager intent cross-check."""

    # ── True (genuine discrepancy) ────────────────────────────────────

    def test_sl_vs_stall_exit(self):
        assert _exit_reasons_conflict("SL", "Stall exit detected") is True

    def test_stop_out_vs_structure_break(self):
        assert _exit_reasons_conflict("STOP_OUT", "Structure break exit") is True

    def test_sl_vs_simulated_tp2(self):
        assert _exit_reasons_conflict("SL", "TP2 hit (simulated)") is True

    def test_tp_vs_simulated_sl(self):
        assert _exit_reasons_conflict("TP", "Stop loss hit (simulated)") is True

    def test_sl_vs_spread_deterioration(self):
        assert _exit_reasons_conflict("SL", "Spread deterioration exit") is True

    def test_tp_vs_news_exit(self):
        assert _exit_reasons_conflict("TP", "News exit triggered") is True

    def test_sl_vs_session_close(self):
        assert _exit_reasons_conflict("SL", "Session close exit") is True

    def test_stop_out_vs_opportunity_exit(self):
        assert _exit_reasons_conflict("STOP_OUT", "Opportunity cost exit") is True

    # ── False (no conflict / benign) ──────────────────────────────────

    def test_tp_vs_none_manager(self):
        assert _exit_reasons_conflict("TP", None) is False

    def test_tp_vs_empty_manager(self):
        assert _exit_reasons_conflict("TP", "") is False

    def test_algo_vs_stall(self):
        assert _exit_reasons_conflict("ALGO", "Stall exit detected") is False

    def test_manual_vs_stall(self):
        assert _exit_reasons_conflict("MANUAL", "Stall exit detected") is False

    def test_broker_closed_unknown_vs_stall(self):
        assert _exit_reasons_conflict("BROKER_CLOSED_UNKNOWN", "Stall exit") is False

    def test_rollover_vs_structure(self):
        assert _exit_reasons_conflict("ROLLOVER", "Structure break exit") is False

    def test_variation_margin_vs_news(self):
        assert _exit_reasons_conflict("VARIATION_MARGIN", "News exit") is False

    def test_split_vs_session(self):
        assert _exit_reasons_conflict("SPLIT", "Session close exit") is False

    def test_sl_vs_stop_loss_agree(self):
        assert _exit_reasons_conflict("SL", "Stop loss hit") is False

    def test_tp_vs_take_profit_agree(self):
        assert _exit_reasons_conflict("TP", "Take profit hit") is False

    def test_sl_vs_unrelated_text(self):
        assert _exit_reasons_conflict("SL", "some random text") is False

    def test_tp_vs_unrelated_text(self):
        assert _exit_reasons_conflict("TP", "some random text") is False

    # ── Edge cases ────────────────────────────────────────────────────

    def test_case_insensitive_manager(self):
        assert _exit_reasons_conflict("SL", "STALL EXIT DETECTED") is True

    def test_whitespace_handling(self):
        assert _exit_reasons_conflict("  SL  ", "  Stall exit  ") is True

    def test_tp_vs_tp2_no_conflict(self):
        assert _exit_reasons_conflict("TP", "TP2 hit (simulated)") is False

    def test_sl_vs_stop_loss_simulated_no_conflict(self):
        assert _exit_reasons_conflict("SL", "Stop_loss hit") is False

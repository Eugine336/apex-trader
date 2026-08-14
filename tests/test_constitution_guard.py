"""CI gate — the constitutional live-path guard must stay green.

Runs ``scripts/constitution_audit.py`` in-process (pure standard library, no
heavy deps) and asserts the live path carries no forbidden construct. In
particular it locks in the V-05/V-06 retirement: the retired directional
deciders (vote/consensus aggregation and the higher-timeframe direction veto,
reachable only through ``DecisionEngine.decide_*`` / ``SituationEngine.assess_*``
/ ``EntryEngine.calculate_entry``) must have NO live call site — re-wiring any of
them onto the live path fails this test.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_GUARD_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "constitution_audit.py"
)


def _load_guard():
    spec = importlib.util.spec_from_file_location("constitution_audit", _GUARD_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: dataclass processing (py3.12) resolves the module
    # via sys.modules[cls.__module__], which must exist during exec_module.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_live_path_is_constitutional():
    guard = _load_guard()
    results = guard.run()
    offenders = {
        check.id: [f"{v.path}:{v.line} {v.snippet}" for v in violations]
        for check, violations in results
        if violations
    }
    assert offenders == {}, f"constitutional violations on the live path: {offenders}"


def test_retired_decider_guard_present_and_has_teeth():
    guard = _load_guard()
    check = next(
        (c for c in guard.CHECKS if c.id == "retired_directional_deciders_removed"),
        None,
    )
    assert check is not None, "the V-05/V-06 retired-decider guard must exist"
    # The bans match a real live call site …
    assert any(rx.search("x = self.decide_entry(ctx)") for rx in check.banned_calls)
    assert any(rx.search("sa = eng.assess_open_trade(ctx)") for rx in check.banned_calls)
    assert any(rx.search("res = self._entry.calculate_entry(scan)") for rx in check.banned_calls)
    # … but not a definition or a bare textual mention.
    assert not any(rx.search("def decide_entry(self, ctx):") for rx in check.banned_calls)
    assert not any(rx.search("# calculate_entry is retired") for rx in check.banned_calls)


def test_guard_flags_a_synthetic_live_call():
    """A retired-decider call planted in a production-looking file is caught."""
    guard = _load_guard()
    check = next(
        c for c in guard.CHECKS if c.id == "retired_directional_deciders_removed"
    )
    sample = "        verdict = self._decision_engine.decide_management(pos, ctx)"
    assert any(rx.search(sample) for rx in check.banned_calls)

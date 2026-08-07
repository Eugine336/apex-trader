"""Tests for the management translator (Brain manage() verdict → action)."""

from types import SimpleNamespace

from cognition.contracts import DecisionType
from cognition.management_translator import ManagementAction, translate_management


def _out(decision_type, symbol="XAUUSD", confidence=0.7):
    dec = SimpleNamespace(
        decision_type=decision_type, symbol=symbol, confidence=confidence,
        thesis="because reasons", decision_id="dec-1",
    )
    return SimpleNamespace(decision=dec)


def _pos(direction="LONG", symbol="XAUUSD"):
    return SimpleNamespace(symbol=symbol, direction=direction)


def test_exit_maps_to_close():
    a = translate_management(_out(DecisionType.EXIT), _pos())
    assert a is not None and a.kind == "close" and a.symbol == "XAUUSD"


def test_terminate_maps_to_close():
    a = translate_management(_out(DecisionType.TERMINATE_CAMPAIGN), _pos())
    assert a.kind == "close"


def test_reverse_sets_target_direction():
    a = translate_management(_out(DecisionType.REVERSE), _pos("LONG"))
    assert a.kind == "reverse" and a.target_direction == "SHORT"
    a2 = translate_management(_out(DecisionType.REVERSE), _pos("SHORT"))
    assert a2.target_direction == "LONG"


def test_scale_out_maps_to_partial_close_with_fraction():
    a = translate_management(_out(DecisionType.SCALE_OUT), _pos(), scale_out_fraction=0.5)
    assert a.kind == "partial_close" and 0.05 <= a.fraction <= 0.95


def test_protect_and_tighten_map_to_sl_moves():
    assert translate_management(_out(DecisionType.PROTECT_PROFIT), _pos()).kind == "protect_sl"
    assert translate_management(_out(DecisionType.TIGHTEN_RISK), _pos()).kind == "tighten_sl"


def test_scale_in_maps_through():
    assert translate_management(_out(DecisionType.SCALE_IN), _pos()).kind == "scale_in"


def test_hold_and_observe_are_none():
    assert translate_management(_out(DecisionType.HOLD), _pos()) is None
    assert translate_management(_out(DecisionType.CONTINUE_OBSERVING), _pos()) is None
    assert translate_management(_out(DecisionType.OPEN_CAMPAIGN), _pos()) is None


def test_fault_safe_on_garbage():
    assert translate_management(None, None) is None
    assert translate_management(object(), object()) is None
    assert translate_management(_out(DecisionType.EXIT, symbol=""), _pos(symbol="")) is None


def test_action_to_dict_is_bounded():
    a = translate_management(_out(DecisionType.EXIT), _pos())
    d = a.to_dict()
    assert d["kind"] == "close" and d["symbol"] == "XAUUSD"
    assert isinstance(a, ManagementAction)

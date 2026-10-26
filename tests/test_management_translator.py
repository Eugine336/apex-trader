"""Tests for the management translator (Brain manage() verdict → action)."""

from types import SimpleNamespace

from cognition.contracts import DecisionType
from cognition.management_translator import ManagementAction, translate_management
from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION


def _out(decision_type, symbol="XAUUSD", confidence=0.7):
    dec = SimpleNamespace(
        decision_type=decision_type, symbol=symbol, confidence=confidence,
        thesis="because reasons", decision_id="dec-1",
        identity_version=IDENTITY_VERSION, identity=IDENTITY_STATEMENT,
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


def test_noncanonical_management_identity_is_not_rewritten():
    output = _out(DecisionType.EXIT)
    output.decision.identity_version = "old-version"

    assert translate_management(output, _pos()) is None


def test_action_to_dict_is_bounded():
    a = translate_management(_out(DecisionType.EXIT), _pos())
    d = a.to_dict()
    assert d["kind"] == "close" and d["symbol"] == "XAUUSD"
    assert isinstance(a, ManagementAction)
    assert d["identity_version"] == IDENTITY_VERSION
    assert d["identity"] == IDENTITY_STATEMENT
    assert d["decision_id"] == "dec-1"


def test_management_action_preserves_campaign_and_opportunity_provenance():
    pos = SimpleNamespace(
        symbol="XAUUSD", direction="LONG", campaign_id="campaign-7",
        opportunity_id="opportunity-9",
    )
    action = translate_management(_out(DecisionType.EXIT), pos)
    assert action is not None
    assert action.decision_id == "dec-1"
    assert action.campaign_id == "campaign-7"
    assert action.opportunity_id == "opportunity-9"
    payload = action.to_dict()
    assert payload["campaign_id"] == "campaign-7"
    assert payload["opportunity_id"] == "opportunity-9"

def test_management_action_prefers_brain_decision_opportunity_provenance():
    output = _out(DecisionType.EXIT)
    output.decision.preferred_opportunity_id = "brain-opportunity-12"
    output.decision.campaign_id = "brain-campaign-4"
    position = _pos()
    position.opportunity_id = "position-opportunity-old"
    position.campaign_id = "position-campaign-old"

    action = translate_management(output, position)

    assert action is not None
    assert action.identity_version == IDENTITY_VERSION
    assert action.identity == IDENTITY_STATEMENT
    assert action.decision_id == "dec-1"
    assert action.campaign_id == "brain-campaign-4"
    assert action.opportunity_id == "brain-opportunity-12"

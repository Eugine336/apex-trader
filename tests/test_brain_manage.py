"""Phase F tests — Brain-driven campaign management (Constitution Parts V, VI.4/5)."""

from types import SimpleNamespace

from cognition.brain import CognitiveBrain, PositionView
from cognition.contracts import DecisionType, Evidence, MarketState
from cognition.gate import MODE_AUTHORITATIVE, MODE_SHADOW, MODE_VETO
from cognition.management_gate import ManagementGate, classify_action


class _Opinion:
    def __init__(self, direction, confidence, rationale="r"):
        self.direction = direction
        self.confidence = confidence
        self.rationale = rationale
        self.competing_hypotheses = []
        self.missing_information = []


class _Reasoner:
    def __init__(self, opinion, available=True):
        self._opinion = opinion
        self._available = available

    @property
    def available(self):
        return self._available

    def reason(self, symbol, evidence, now=None):
        return self._opinion


def _state(symbol="EURUSD", polarity=0.8):
    ms = MarketState(symbol=symbol)
    ms.add(Evidence(source_module="s", confidence=0.9, uncertainty=0.1, polarity=polarity))
    return ms


def _pos(direction="LONG", profit_r=0.0):
    return PositionView(symbol="EURUSD", direction=direction, profit_r=profit_r)


# ── Brain.manage ─────────────────────────────────────────────────────────────

def test_manage_holds_when_thesis_intact():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)))
    out = brain.manage(_pos("LONG"), _state(polarity=0.8))
    assert out.decision.decision_type == DecisionType.HOLD


def test_manage_tightens_when_weakening():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.4)),
                           min_confidence_to_act=0.55, exit_floor=0.3)
    out = brain.manage(_pos("LONG"), _state(polarity=0.8))
    assert out.decision.decision_type == DecisionType.TIGHTEN_RISK


def test_manage_holds_when_flat_read_is_not_invalidation():
    # Flaw 1 fix (constitutional rule): a FLAT / uncertain read is NOT a reasoned
    # invalidation of the campaign thesis, so it must HOLD — never auto-EXIT.
    # Previously this returned EXIT via the catch-all; that was the bug.
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("FLAT", 0.9)))
    out = brain.manage(_pos("LONG"), _state(polarity=0.0))
    assert out.decision.decision_type == DecisionType.HOLD


def test_manage_exits_on_explicit_opportunity_gone():
    # An EXIT still fires when the (aligned) opinion EXPLICITLY says the
    # opportunity is gone — a reasoned deterioration, not bare uncertainty.
    op = _Opinion("LONG", 0.8)
    op.opportunity = "none"
    op.expected_value = "negative — cost exceeds edge"
    brain = CognitiveBrain(reasoner=_Reasoner(op))
    out = brain.manage(_pos("LONG"), _state(polarity=0.8))
    assert out.decision.decision_type == DecisionType.EXIT


def test_manage_reverses_on_strong_contrary():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("SHORT", 0.85)), reverse_confidence=0.7)
    out = brain.manage(_pos("LONG"), _state(polarity=-0.8))
    assert out.decision.decision_type == DecisionType.REVERSE
    assert out.direction == "SHORT"          # flipped


def test_manage_exits_on_mild_contrary():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("SHORT", 0.6)),
                           min_confidence_to_act=0.55, reverse_confidence=0.7)
    out = brain.manage(_pos("LONG"), _state(polarity=-0.5))
    assert out.decision.decision_type == DecisionType.EXIT


def test_manage_scale_in_when_allowed_and_profitable():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.9)),
                           allow_scale_in=True, reverse_confidence=0.7)
    out = brain.manage(_pos("LONG", profit_r=0.5), _state(polarity=0.8))
    assert out.decision.decision_type == DecisionType.SCALE_IN


def test_manage_holds_without_reasoner():
    brain = CognitiveBrain(reasoner=None)
    out = brain.manage(_pos("LONG"), _state())
    assert out.decision.decision_type == DecisionType.HOLD
    assert brain.get_status()["managed"] == 1
    assert brain.latest_management("EURUSD") is not None


# ── ManagementGate ───────────────────────────────────────────────────────────

def test_classify_action():
    assert classify_action("scale_in") == "add"
    assert classify_action("re_entry") == "add"
    assert classify_action("exit") == "reduce"
    assert classify_action("tighten_risk") == "reduce"
    assert classify_action("something_new") == "other"


def _brain_latest(direction="LONG", authorises=True):
    import time
    decision = SimpleNamespace(decision_type=SimpleNamespace(value="open_campaign"),
                               authorises_action=authorises)
    output = SimpleNamespace(decision=decision, direction=direction,
                             decided_at_epoch=time.time())
    return SimpleNamespace(latest=lambda s: output)


def test_management_gate_never_blocks_de_risking():
    # Even in authoritative mode with a hostile Brain read, exits are allowed.
    g = ManagementGate(_brain_latest("SHORT"), mode=MODE_AUTHORITATIVE)
    assert g.evaluate("EURUSD", "LONG", "exit").allow is True
    assert g.evaluate("EURUSD", "LONG", "scale_out").allow is True
    assert g.evaluate("EURUSD", "LONG", "tighten_risk").allow is True


def test_management_gate_gates_adds_veto():
    # Risk-adding blocked when the Brain doesn't back the direction (veto mode).
    g = ManagementGate(_brain_latest("SHORT"), mode=MODE_VETO)
    assert g.evaluate("EURUSD", "LONG", "scale_in").allow is False
    st = g.get_status()
    assert st["add_evaluations"] == 1 and st["add_blocked"] == 1


def test_management_gate_allows_adds_when_brain_backs():
    g = ManagementGate(_brain_latest("LONG"), mode=MODE_VETO)
    assert g.evaluate("EURUSD", "LONG", "scale_in").allow is True


def test_management_gate_shadow_allows_adds_but_records():
    g = ManagementGate(_brain_latest("SHORT"), mode=MODE_SHADOW)
    v = g.evaluate("EURUSD", "LONG", "scale_in")
    assert v.allow is True and v.would_veto is True


def test_management_gate_authoritative_add_fails_closed_without_brain():
    g = ManagementGate(None, mode=MODE_AUTHORITATIVE)
    assert g.evaluate("EURUSD", "LONG", "scale_in").allow is False
    # de-risking still allowed even with no brain
    assert g.evaluate("EURUSD", "LONG", "exit").allow is True

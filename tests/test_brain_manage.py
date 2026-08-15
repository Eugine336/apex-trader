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


# ── Brain.manage — V-008: incomplete management data holds (no direction fallback) ──
# A management opinion carrying NONE of thesis_state / opportunity_status /
# management_action is degraded/incomplete data. The Brain HOLDS the current
# state and never falls back to a direction/confidence comparison against the
# held side (the removed legacy path could SCALE_IN / TIGHTEN / EXIT / REVERSE
# from that comparison — a directional authority the constitution forbids for
# management). Thesis-based EXIT / REVERSE / SCALE_IN / TIGHTEN on real
# management fields is covered in test_brain_manage_thesis.py.

def test_manage_holds_without_reasoner():
    brain = CognitiveBrain(reasoner=None)
    out = brain.manage(_pos("LONG"), _state())
    assert out.decision.decision_type == DecisionType.HOLD
    assert brain.get_status()["managed"] == 1
    assert brain.latest_management("EURUSD") is not None


def test_manage_holds_when_aligned_direction_lacks_management_fields():
    # Aligned high-confidence direction read but no management fields → degraded
    # data → HOLD (previously this could SCALE_IN / hold-as-intact via direction).
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.9)),
                           allow_scale_in=True, reverse_confidence=0.7)
    out = brain.manage(_pos("LONG", profit_r=1.0), _state(polarity=0.8))
    assert out.decision.decision_type == DecisionType.HOLD


def test_manage_holds_when_contrary_direction_lacks_management_fields():
    # A strong CONTRARY direction read must NOT reverse or exit a live campaign
    # when the reply carries no management fields — no direction comparison.
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("SHORT", 0.95)),
                           min_confidence_to_act=0.55, reverse_confidence=0.7)
    out = brain.manage(_pos("LONG"), _state(polarity=-0.9))
    assert out.decision.decision_type == DecisionType.HOLD
    assert out.direction == "LONG"          # never flipped


def test_manage_holds_when_flat_read_is_not_invalidation():
    # A FLAT / uncertain read is NOT a reasoned invalidation → HOLD, never EXIT.
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("FLAT", 0.9)))
    out = brain.manage(_pos("LONG"), _state(polarity=0.0))
    assert out.decision.decision_type == DecisionType.HOLD


def test_manage_holds_when_opportunity_signal_lacks_management_fields():
    # Even an explicit opportunity/EV signal is degraded management data without
    # the canonical management fields → HOLD (the thesis path owns EXIT-on-EV).
    op = _Opinion("LONG", 0.8)
    op.opportunity = "none"
    op.expected_value = "negative — cost exceeds edge"
    brain = CognitiveBrain(reasoner=_Reasoner(op))
    out = brain.manage(_pos("LONG"), _state(polarity=0.8))
    assert out.decision.decision_type == DecisionType.HOLD


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

"""Tests for the AI Cognitive Brain entry gate (Step C) — Constitution Part VI."""

from types import SimpleNamespace

from cognition.gate import MODE_OFF, MODE_SHADOW, MODE_VETO, CognitionGate, normalise_mode


def _brain(latest):
    return SimpleNamespace(latest=lambda symbol: latest)


def _output(direction="LONG", authorises=True, dtype="open_campaign"):
    decision = SimpleNamespace(
        decision_type=SimpleNamespace(value=dtype),
        authorises_action=authorises,
    )
    return SimpleNamespace(decision=decision, direction=direction)


def test_normalise_mode():
    assert normalise_mode("VETO") == MODE_VETO
    assert normalise_mode("nonsense") == MODE_SHADOW
    assert normalise_mode("off") == MODE_OFF


def test_off_mode_always_allows():
    g = CognitionGate(_brain(_output("SHORT")), mode=MODE_OFF)
    assert g.evaluate("EURUSD", "LONG").allow is True


def test_no_brain_allows():
    assert CognitionGate(None, mode=MODE_VETO).evaluate("EURUSD", "LONG").allow is True


def test_cold_start_no_read_fails_open():
    g = CognitionGate(_brain(None), mode=MODE_VETO)
    v = g.evaluate("EURUSD", "LONG")
    assert v.allow is True and v.aligned is None


def test_shadow_records_would_veto_but_allows():
    g = CognitionGate(_brain(_output("SHORT", authorises=True)), mode=MODE_SHADOW)
    v = g.evaluate("EURUSD", "LONG")     # brain backs SHORT, entry is LONG → misaligned
    assert v.allow is True               # shadow always allows
    assert v.would_veto is True
    assert g.get_status()["would_veto"] == 1
    assert g.get_status()["vetoed"] == 0


def test_veto_blocks_when_brain_disagrees():
    g = CognitionGate(_brain(_output("SHORT", authorises=True)), mode=MODE_VETO)
    v = g.evaluate("EURUSD", "LONG")
    assert v.allow is False
    assert g.get_status()["vetoed"] == 1


def test_veto_allows_when_brain_agrees():
    g = CognitionGate(_brain(_output("LONG", authorises=True)), mode=MODE_VETO)
    v = g.evaluate("EURUSD", "LONG")
    assert v.allow is True and v.aligned is True and v.would_veto is False


def test_veto_blocks_when_brain_says_observe():
    # Brain read exists but does not authorise action (CONTINUE_OBSERVING).
    g = CognitionGate(_brain(_output("FLAT", authorises=False, dtype="continue_observing")),
                      mode=MODE_VETO)
    assert g.evaluate("EURUSD", "LONG").allow is False


def test_gate_fault_fails_open():
    class _BadBrain:
        def latest(self, symbol):
            raise RuntimeError("boom")

    g = CognitionGate(_BadBrain(), mode=MODE_VETO)
    assert g.evaluate("EURUSD", "LONG").allow is True

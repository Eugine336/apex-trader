"""Tests for the AI Cognitive Brain entry gate (Steps C/D) — Constitution Part VI."""

import time
from types import SimpleNamespace

from cognition.gate import (
    MODE_AUTHORITATIVE,
    MODE_OFF,
    MODE_SHADOW,
    MODE_VETO,
    CognitionGate,
    normalise_mode,
)


def _brain(latest):
    return SimpleNamespace(latest=lambda symbol: latest)


def _output(direction="LONG", authorises=True, dtype="open_campaign", decided_at_epoch=None):
    decision = SimpleNamespace(
        decision_type=SimpleNamespace(value=dtype),
        authorises_action=authorises,
    )
    return SimpleNamespace(
        decision=decision, direction=direction,
        decided_at_epoch=time.time() if decided_at_epoch is None else decided_at_epoch,
    )


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


# ── Step D: authoritative mode (Brain = sole decider, fail-closed) ────────────

def test_authoritative_no_brain_fails_closed():
    assert CognitionGate(None, mode=MODE_AUTHORITATIVE).evaluate("EURUSD", "LONG").allow is False


def test_authoritative_cold_start_fails_closed():
    # No decision yet ⇒ no trade (unlike veto, which fails open on cold start).
    assert CognitionGate(_brain(None), mode=MODE_AUTHORITATIVE).evaluate("EURUSD", "LONG").allow is False


def test_authoritative_allows_when_brain_authorises_fresh():
    g = CognitionGate(_brain(_output("LONG", authorises=True)), mode=MODE_AUTHORITATIVE)
    v = g.evaluate("EURUSD", "LONG")
    assert v.allow is True and v.aligned is True


def test_authoritative_blocks_on_disagreement():
    g = CognitionGate(_brain(_output("SHORT", authorises=True)), mode=MODE_AUTHORITATIVE)
    assert g.evaluate("EURUSD", "LONG").allow is False


def test_authoritative_blocks_on_observe():
    g = CognitionGate(_brain(_output("FLAT", authorises=False, dtype="continue_observing")),
                      mode=MODE_AUTHORITATIVE)
    assert g.evaluate("EURUSD", "LONG").allow is False


def test_authoritative_blocks_stale_authorization():
    # A fresh, aligned OPEN_CAMPAIGN but decided long ago ⇒ stale ⇒ blocked
    # (renewed authorization per action).
    old = 1_000.0
    g = CognitionGate(_brain(_output("LONG", authorises=True, decided_at_epoch=old)),
                      mode=MODE_AUTHORITATIVE, max_decision_age_seconds=60.0)
    assert g.evaluate("EURUSD", "LONG", now=old + 120.0).allow is False
    # Within the freshness window it is allowed.
    assert g.evaluate("EURUSD", "LONG", now=old + 30.0).allow is True


def test_authoritative_fault_fails_closed():
    class _BadBrain:
        def latest(self, symbol):
            raise RuntimeError("boom")

    assert CognitionGate(_BadBrain(), mode=MODE_AUTHORITATIVE).evaluate("EURUSD", "LONG").allow is False


def test_status_reports_authoritative():
    g = CognitionGate(_brain(_output()), mode=MODE_AUTHORITATIVE)
    st = g.get_status()
    assert st["authoritative"] is True and st["mode"] == "authoritative"

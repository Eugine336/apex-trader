"""Tests for the per-gate parameter attribution (GateAttributor + Governance wiring)."""

from __future__ import annotations

import threading

import pytest

from adaptive.gate_attribution import (
    GateAttribution,
    GateAttributor,
    GateImpact,
    GateSnapshot,
)
from governance.division import GovernanceDivision


class _Cfg:
    """Minimal stand-in for EntryConfig defaults."""

    min_entry_ev = 0.3
    min_entry_score = 85
    min_htf_alignment = -0.5


def _attributor() -> GateAttributor:
    return GateAttributor(gate_tuner=None, config=_Cfg())


# ── (a) decisive attribution ─────────────────────────────────────────────


def test_decisive_attribution():
    """entry_engine loosened + score below the default bar → DECISIVE."""
    ga = _attributor()
    ga.record_entry(
        "o1",
        gate_offsets={"entry_engine": -3.0, "ev_gate": 0.0, "htf_alignment": 0.0},
        effective_thresholds={"entry_engine": 82.0, "ev_gate": 0.3, "htf_alignment": -0.5},
    )
    att = ga.record_close("o1", realized_r=-1.0, entry_ev=0.4, entry_score=83.0)
    assert att is not None
    assert att.per_gate["entry_engine"] == GateImpact.DECISIVE.value
    assert att.learner_decisive is True


# ── (b) supporting attribution ───────────────────────────────────────────


def test_supporting_attribution():
    """entry_engine loosened but score clears the default bar anyway → SUPPORTING."""
    ga = _attributor()
    ga.record_entry(
        "o2",
        gate_offsets={"entry_engine": -3.0},
        effective_thresholds={"entry_engine": 82.0},
    )
    att = ga.record_close("o2", realized_r=2.0, entry_ev=0.4, entry_score=90.0)
    assert att is not None
    assert att.per_gate["entry_engine"] == GateImpact.SUPPORTING.value
    assert att.learner_decisive is False


# ── (c) neutral when no offsets ──────────────────────────────────────────


def test_neutral_when_no_offsets():
    ga = _attributor()
    ga.record_entry(
        "o3",
        gate_offsets={"entry_engine": 0.0, "ev_gate": 0.0, "htf_alignment": 0.0},
        effective_thresholds={"entry_engine": 85.0, "ev_gate": 0.3, "htf_alignment": -0.5},
    )
    att = ga.record_close("o3", realized_r=1.0, entry_ev=0.5, entry_score=88.0)
    assert att is not None
    assert all(v == GateImpact.NEUTRAL.value for v in att.per_gate.values())
    assert att.learner_decisive is False


def test_htf_alignment_never_decisive():
    """htf_alignment has no replay input → NEUTRAL even with a non-zero offset."""
    ga = _attributor()
    ga.record_entry(
        "ohtf",
        gate_offsets={"htf_alignment": -0.3},
        effective_thresholds={"htf_alignment": -0.8},
    )
    att = ga.record_close("ohtf", realized_r=-1.0, entry_ev=0.4, entry_score=90.0)
    assert att is not None
    assert att.per_gate["htf_alignment"] == GateImpact.NEUTRAL.value
    assert att.learner_decisive is False


def test_ev_gate_proxy_decisive_when_loosened():
    """The close path supplies the effective EV threshold as the proxy; a
    loosened ev_gate (effective < default) reads as DECISIVE."""
    ga = _attributor()
    ga.record_entry(
        "o4",
        gate_offsets={"ev_gate": -0.05},
        effective_thresholds={"ev_gate": 0.25},
    )
    att = ga.record_close("o4", realized_r=-0.5, entry_ev=0.25, entry_score=90.0)
    assert att is not None
    assert att.per_gate["ev_gate"] == GateImpact.DECISIVE.value
    assert att.learner_decisive is True


# ── (d) per-gate breakdown ───────────────────────────────────────────────


def test_per_gate_breakdown():
    ga = _attributor()
    # ev_gate decisive (loser)
    ga.record_entry("a", {"ev_gate": -0.05}, {"ev_gate": 0.25})
    ga.record_close("a", realized_r=-0.5, entry_ev=0.25, entry_score=90.0)
    # entry_engine decisive (winner)
    ga.record_entry("b", {"entry_engine": -3.0}, {"entry_engine": 82.0})
    ga.record_close("b", realized_r=1.5, entry_ev=0.5, entry_score=83.0)
    # entry_engine supporting
    ga.record_entry("c", {"entry_engine": -3.0}, {"entry_engine": 82.0})
    ga.record_close("c", realized_r=0.4, entry_ev=0.5, entry_score=90.0)

    summary = ga.summarize()
    assert summary["total_closed"] == 3
    assert summary["decisive_count"] == 2
    assert summary["supporting_count"] == 1
    per = summary["per_gate"]
    assert per["ev_gate"]["decisive_count"] == 1
    assert per["entry_engine"]["decisive_count"] == 1
    assert per["entry_engine"]["supporting_count"] == 1
    # decisive_negative_ev_count: one decisive loser (ev_gate trade at -0.5)
    assert summary["decisive_negative_ev_count"] == 1


# ── (e) thread safety ────────────────────────────────────────────────────


def test_thread_safety():
    ga = _attributor()

    def worker(start: int):
        for i in range(start, start + 100):
            oid = f"t{i}"
            ga.record_entry(oid, {"entry_engine": -3.0}, {"entry_engine": 82.0})
            ga.record_close(oid, realized_r=1.0, entry_ev=0.4, entry_score=83.0)

    threads = [threading.Thread(target=worker, args=(n * 100,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    summary = ga.summarize()
    # deque maxlen default 200; all closes were decisive
    assert summary["total_closed"] == 200
    assert summary["decisive_count"] == 200


# ── (f) fail-safe ────────────────────────────────────────────────────────


def test_fail_safe_record_close_bad_input_returns_none():
    ga = _attributor()
    ga.record_entry("x", {"entry_engine": -3.0}, {"entry_engine": 82.0})
    # A non-numeric realized_r forces float() to raise inside record_close.
    out = ga.record_close("x", realized_r=object(), entry_ev=0.4, entry_score=83.0)  # type: ignore[arg-type]
    assert out is None


def test_fail_safe_record_entry_returns_none_on_fault():
    ga = _attributor()
    # gate_offsets that cannot be float()-ed → caught, returns None.
    out = ga.record_entry("y", {"entry_engine": object()}, {})  # type: ignore[dict-item]
    assert out is None


def test_summarize_never_raises():
    ga = _attributor()
    # Even with nothing recorded, summarize returns a well-formed dict.
    assert isinstance(ga.summarize(), dict)


# ── (g) learner_enabled flag ─────────────────────────────────────────────


def test_learner_enabled_flag():
    ga = _attributor()
    snap_on = ga.record_entry("on", {"entry_engine": -1.0, "ev_gate": 0.0}, {})
    snap_off = ga.record_entry("off", {"entry_engine": 0.0, "ev_gate": 0.0}, {})
    assert snap_on is not None and snap_on.learner_enabled is True
    assert snap_off is not None and snap_off.learner_enabled is False


# ── (h) decisive-negative-EV warning ─────────────────────────────────────


class _StubAttributor:
    def __init__(self, summary: dict):
        self._summary = summary

    def summarize(self) -> dict:
        return self._summary


def test_decisive_negative_ev_warning(monkeypatch):
    """Governance warns when ≥3 learner-decisive trades close negative."""
    import governance.division as gd

    calls: list[str] = []

    class _Logger:
        def warning(self, msg, *args, **kwargs):
            calls.append(str(msg))

        def __getattr__(self, _n):
            return lambda *a, **k: None

    monkeypatch.setattr(gd, "logger", _Logger())

    gov = GovernanceDivision(
        gate_attributor=_StubAttributor(
            {"decisive_negative_ev_count": 4, "decisive_avg_r": -0.8}
        )
    )
    gov._warn_on_decisive_losses()
    assert any("learner-decisive" in c for c in calls)


def test_no_warning_below_threshold(monkeypatch):
    import governance.division as gd

    calls: list[str] = []

    class _Logger:
        def warning(self, msg, *args, **kwargs):
            calls.append(str(msg))

        def __getattr__(self, _n):
            return lambda *a, **k: None

    monkeypatch.setattr(gd, "logger", _Logger())

    gov = GovernanceDivision(
        gate_attributor=_StubAttributor(
            {"decisive_negative_ev_count": 1, "decisive_avg_r": -0.2}
        )
    )
    gov._warn_on_decisive_losses()
    assert not calls


# ── (i) summarize empty ──────────────────────────────────────────────────


def test_summarize_empty():
    ga = _attributor()
    summary = ga.summarize()
    assert summary["total_closed"] == 0
    assert summary["decisive_count"] == 0
    assert summary["supporting_count"] == 0
    assert summary["decisive_avg_r"] == 0.0
    assert summary["supporting_avg_r"] == 0.0
    assert summary["decisive_negative_ev_count"] == 0
    assert summary["per_gate"] == {
        "ev_gate": {"decisive_count": 0, "supporting_count": 0, "decisive_avg_r": 0.0},
        "entry_engine": {"decisive_count": 0, "supporting_count": 0, "decisive_avg_r": 0.0},
        "htf_alignment": {"decisive_count": 0, "supporting_count": 0, "decisive_avg_r": 0.0},
    }


# ── (j) Governance wiring surfaces gate attribution ──────────────────────


def test_governance_get_status_surfaces_gate_attribution():
    summary = {"total_closed": 5, "decisive_count": 2}
    gov = GovernanceDivision(gate_attributor=_StubAttributor(summary))
    status = gov.get_status()
    assert status["gate_attribution"] == summary


def test_governance_get_status_none_without_attributor():
    gov = GovernanceDivision()
    assert gov.get_status()["gate_attribution"] is None


def test_governance_bind_runtime_accepts_gate_attributor():
    gov = GovernanceDivision()
    stub = _StubAttributor({"total_closed": 0})
    gov.bind_runtime(gate_attributor=stub)
    assert gov.get_status()["gate_attribution"] == {"total_closed": 0}


def test_missing_snapshot_close_returns_none():
    ga = _attributor()
    assert ga.record_close("never-opened", realized_r=1.0, entry_ev=0.4, entry_score=90.0) is None


def test_reset_clears_state():
    ga = _attributor()
    ga.record_entry("r", {"entry_engine": -3.0}, {"entry_engine": 82.0})
    ga.record_close("r", realized_r=1.0, entry_ev=0.4, entry_score=83.0)
    assert ga.summarize()["total_closed"] == 1
    ga.reset()
    assert ga.summarize()["total_closed"] == 0

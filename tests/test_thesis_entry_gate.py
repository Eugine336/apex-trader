"""Session 25 — wiring the ThesisEngine into the entry pipeline (Gap 1b).

Covers two integration changes:

1. ``counter_trend_ev_premium`` is fully removed — the EV gate no longer adds a
   directional surcharge, so a counter-trend idea faces the SAME EV bar as a
   with-trend one and passes on its own merits.
2. ``EventDrivenSystem._thesis_gate_allows`` — the ThesisEngine acts as an
   additional entry quality gate that blocks when the dominant thesis disagrees
   with the proposed direction or is not actionable, and is fully fail-safe
   (allows the entry when the engine is absent, the symbol has no thesis yet, or
   anything errors).

The bare-shell ``EventDrivenSystem.__new__`` pattern (no ``__init__``) mirrors
the existing multi-opportunity / learning-loop wiring tests.
"""

from types import SimpleNamespace

from brain.thesis_engine import ThesisEngine
from entry.entry_gate import EntryGate
from entry.models import EntryConfig


# ── Vote stub (module / direction / confidence / weight) ─────────────────────


def _vote(module: str, direction: str, confidence: float, weight: float):
    return SimpleNamespace(
        module=module, direction=direction, confidence=confidence, weight=weight,
    )


# ── Task 1: counter_trend_ev_premium is gone ────────────────────────────────


class TestCounterTrendPremiumRemoved:
    def test_config_has_no_premium_field(self):
        cfg = EntryConfig()
        assert not hasattr(cfg, "counter_trend_ev_premium")

    def test_counter_trend_between_base_and_old_premium_now_passes(self):
        # p_win=0.45 (LONG), rr=2.0, p_loss=0.55 → EV = 0.9 - 0.55 = 0.35R.
        # Counter-trend (p_loss > p_win). There is no directional premium — a
        # counter-trend idea faces the SAME EV bar (0.1R) as with-trend, so a
        # 0.35R setup passes on its own merits.
        gate = EntryGate()
        r = gate._check_ev("EURUSD", "LONG", 100.0, 99.0, 102.0, 0.45, 0.55)
        assert r.passed is True
        assert "counter-trend" in r.reason

    def test_with_trend_and_counter_trend_share_the_same_bar(self):
        # Same EV magnitude (0.35R) with-trend also passes — no asymmetry.
        gate = EntryGate()
        r = gate._check_ev("EURUSD", "LONG", 100.0, 99.0, 102.0, 0.55, 0.45)
        # EV = 0.55*2 - 0.45 = 0.65R, with-trend, passes.
        assert r.passed is True
        assert "with-trend" in r.reason


# ── Task 2: ThesisEngine entry gate ──────────────────────────────────────────


def _shell(thesis_engine=None, gate_enabled=True):
    """An EventDrivenSystem shell exposing only what _thesis_gate_allows reads."""
    from event_driven_bootstrap import EventDrivenSystem

    sys = EventDrivenSystem.__new__(EventDrivenSystem)
    sys._ctx = SimpleNamespace(thesis_engine=thesis_engine)
    sys._config = SimpleNamespace(thesis=SimpleNamespace(gate_enabled=gate_enabled))
    return sys


def _long_dominant_engine():
    """A ThesisEngine whose EURUSD dominant thesis is LONG, clearing the bar."""
    eng = ThesisEngine(min_ev_threshold=0.1, flat_ev=0.0)
    eng.update(
        symbol="EURUSD",
        votes=[_vote("structure", "LONG", 0.8, 2.0),
               _vote("momentum", "LONG", 0.7, 1.0)],
        long_probability=0.7,
        short_probability=0.3,
        entry_ev_long=0.5,    # dominant, well over flat (0.0) + threshold (0.1)
        entry_ev_short=-0.2,
    )
    return eng


class TestThesisGate:
    def test_allows_when_thesis_agrees(self):
        sys = _shell(_long_dominant_engine())
        assert sys._thesis_gate_allows("EURUSD", "LONG") is True

    def test_blocks_when_thesis_disagrees(self):
        # Dominant thesis is LONG; a SHORT entry must be skipped.
        sys = _shell(_long_dominant_engine())
        assert sys._thesis_gate_allows("EURUSD", "SHORT") is False

    def test_blocks_when_not_actionable(self):
        # Both directional EVs below the opportunity-cost margin → Flat dominates
        # / no actionable thesis → block.
        eng = ThesisEngine(min_ev_threshold=0.1, flat_ev=0.0)
        eng.update(
            symbol="EURUSD",
            votes=[_vote("structure", "LONG", 0.5, 1.0)],
            long_probability=0.5, short_probability=0.5,
            entry_ev_long=0.05,   # below 0.1 margin
            entry_ev_short=-0.3,
        )
        sys = _shell(eng)
        assert sys._thesis_gate_allows("EURUSD", "LONG") is False

    def test_allows_unknown_symbol_failsafe(self):
        # Engine present but this symbol was never fed → no thesis tracked →
        # cannot gate on absent evidence → allow.
        sys = _shell(_long_dominant_engine())
        assert sys._thesis_gate_allows("GBPUSD", "LONG") is True

    def test_allows_when_engine_absent(self):
        sys = _shell(thesis_engine=None)
        assert sys._thesis_gate_allows("EURUSD", "LONG") is True

    def test_allows_when_gate_disabled(self):
        # Disagreeing thesis would normally block, but the gate is off → allow.
        sys = _shell(_long_dominant_engine(), gate_enabled=False)
        assert sys._thesis_gate_allows("EURUSD", "SHORT") is True

    def test_allows_on_engine_error_failsafe(self):
        class _Boom:
            def get(self, symbol):
                return object()  # truthy → past the cold-start guard

            def should_act(self, symbol):
                raise RuntimeError("boom")

        sys = _shell(_Boom())
        assert sys._thesis_gate_allows("EURUSD", "LONG") is True

    def test_direction_comparison_is_case_insensitive(self):
        sys = _shell(_long_dominant_engine())
        assert sys._thesis_gate_allows("EURUSD", "long") is True

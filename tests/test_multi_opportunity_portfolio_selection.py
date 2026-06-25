"""Session 3 — Multi-Opportunity Portfolio Selection.

Verifies the capital-allocation layer that decides WHICH independent candidates
receive capital (Portfolio Division), instead of ratifying one pre-collapsed
direction:

* ``PortfolioGovernor.allocate`` returns an :class:`Allocation` and enforces the
  five gates — V2 hedge (Gate 0), per-symbol cap (Gate 1), timeframe-class cap
  (Gate 2), global cap (Gate 3), and the total risk budget (Gate 4).
* V2 hedging: an opposing position on the same symbol is allowed but capped to
  ``hedge_ratio_cap`` of the dominant size; an exhausted hedge is rejected.
* ``EventDrivenSystem._select_and_execute`` grades each candidate via the
  orchestrator round table (``grade_candidate`` — live, not just tests), drops
  the uniformly weak, ranks best-first, and funds survivors through
  ``allocate`` so the strongest idea hits the caps first.
* The legacy ``check()`` concentration API is left intact (backward compatible).
"""

from __future__ import annotations

from types import SimpleNamespace

from brain.candidate_models import Allocation, Candidate, CandidateEntryDecision
from brain.orchestrator import Orchestrator
from config import GovernorConfig
from event_driven_bootstrap import EventDrivenSystem
from governor import PortfolioGovernor


# ── helpers ─────────────────────────────────────────────────────────────────


def _pos(symbol: str, direction: str, lots: float = 1.0, risk_pct: float = 0.0,
         timeframe_class: str = "") -> dict:
    return {
        "symbol": symbol, "direction": direction, "lots": lots,
        "risk_pct": risk_pct, "timeframe_class": timeframe_class,
    }


def _cfg(**over) -> GovernorConfig:
    base = dict(
        max_positions_per_symbol=2,
        max_positions_per_tf_class=3,
        max_total_positions=10,
        max_total_risk_pct=6.0,
        per_trade_max_risk=2.0,
        hedge_ratio_cap=0.30,
    )
    base.update(over)
    return GovernorConfig(**base)


# ── 1. allocate() returns an Allocation with the expected fields ─────────────


def test_allocate_returns_allocation_object():
    gov = PortfolioGovernor(_cfg())
    a = gov.allocate(symbol="EURUSD", direction="LONG", timeframe_class="SWING")
    assert isinstance(a, Allocation)
    assert a.approved is True
    assert a.max_risk_pct == 2.0  # full per-trade budget on an empty book
    assert isinstance(a.conflicts, list)


# ── 2. Gate 0 — V2 hedging ───────────────────────────────────────────────────


def test_hedge_opposite_direction_allowed_but_capped():
    gov = PortfolioGovernor(_cfg())
    book = [_pos("EURUSD", "LONG", lots=1.0)]  # dominant LONG swing, 1.0 lot
    a = gov.allocate(
        symbol="EURUSD", direction="SHORT", timeframe_class="SCALP",
        open_positions=book,
    )
    assert a.approved is True
    # Opposing risk budget capped to 30% of per_trade (2.0 * 0.30 = 0.6).
    assert a.max_risk_pct == 0.6
    assert a.conflicts == ["EURUSD LONG"]


def test_hedge_lots_scaled_to_room():
    gov = PortfolioGovernor(_cfg())
    book = [_pos("EURUSD", "LONG", lots=1.0)]  # allowed opposing = 0.30 lot
    a = gov.allocate(
        symbol="EURUSD", direction="SHORT", proposed_lots=0.6,
        open_positions=book,
    )
    # proposed 0.6 lot > room 0.30 → budget scaled by 0.30/0.6 = 0.5 → 0.6*0.5.
    assert a.approved is True
    assert round(a.max_risk_pct, 4) == 0.3


def test_hedge_cap_exhausted_rejects():
    gov = PortfolioGovernor(_cfg(max_positions_per_symbol=5))
    # Dominant LONG 1.0; opposing SHORT already at the 30% cap (0.30).
    book = [_pos("EURUSD", "LONG", lots=1.0), _pos("EURUSD", "SHORT", lots=0.30)]
    a = gov.allocate(symbol="EURUSD", direction="SHORT", open_positions=book)
    assert a.approved is False
    assert a.reason.startswith("hedge_cap_exhausted")


def test_same_direction_not_hedge_capped():
    gov = PortfolioGovernor(_cfg())
    book = [_pos("EURUSD", "LONG", lots=1.0)]
    a = gov.allocate(symbol="EURUSD", direction="LONG", open_positions=book)
    assert a.approved is True
    assert a.max_risk_pct == 2.0  # full budget, no hedge cap on the dominant side


# ── 3. Gate 1 — per-symbol position cap ──────────────────────────────────────


def test_symbol_position_cap_enforced():
    gov = PortfolioGovernor(_cfg(max_positions_per_symbol=2))
    book = [_pos("EURUSD", "LONG"), _pos("EURUSD", "SHORT")]
    a = gov.allocate(symbol="EURUSD", direction="LONG", open_positions=book)
    assert a.approved is False
    assert a.reason.startswith("symbol_cap_hit")


# ── 4. Gate 2 — timeframe-class cap ──────────────────────────────────────────


def test_tf_class_cap_enforced():
    gov = PortfolioGovernor(_cfg(max_positions_per_tf_class=3, max_positions_per_symbol=99))
    book = [
        _pos("EURUSD", "LONG", timeframe_class="SWING"),
        _pos("GBPUSD", "LONG", timeframe_class="SWING"),
        _pos("AUDUSD", "LONG", timeframe_class="SWING"),
    ]
    a = gov.allocate(
        symbol="USDJPY", direction="LONG", timeframe_class="SWING",
        open_positions=book,
    )
    assert a.approved is False
    assert a.reason.startswith("tf_class_cap_hit")


# ── 5. Gate 3 — global position cap ──────────────────────────────────────────


def test_global_position_cap_enforced():
    gov = PortfolioGovernor(_cfg(max_total_positions=3, max_positions_per_symbol=99))
    book = [_pos(f"SYM{i}", "LONG") for i in range(3)]
    a = gov.allocate(symbol="EURUSD", direction="LONG", open_positions=book)
    assert a.approved is False
    assert a.reason.startswith("global_cap_hit")


# ── 6. Gate 4 — total risk budget ────────────────────────────────────────────


def test_risk_budget_exhausted_rejects():
    gov = PortfolioGovernor(_cfg(max_total_risk_pct=6.0, max_positions_per_symbol=99))
    book = [_pos(f"SYM{i}", "LONG", risk_pct=2.0) for i in range(3)]  # 6% used
    a = gov.allocate(symbol="EURUSD", direction="LONG", open_positions=book)
    assert a.approved is False
    assert a.reason.startswith("risk_budget_exhausted")


def test_risk_budget_partial_grant():
    gov = PortfolioGovernor(_cfg(max_total_risk_pct=6.0, max_positions_per_symbol=99))
    book = [_pos(f"SYM{i}", "LONG", risk_pct=2.0) for i in range(2)]  # 4% used, 2% left
    a = gov.allocate(symbol="EURUSD", direction="LONG", open_positions=book)
    assert a.approved is True
    assert a.max_risk_pct == 2.0  # min(per_trade 2.0, remaining 2.0)


# ── 7. allocate() fails closed on error ──────────────────────────────────────


def test_allocate_fails_closed_on_error():
    gov = PortfolioGovernor(_cfg())
    # A position object that raises when its attributes are read still must not
    # leak risk through — fail-closed by default.
    class _Boom:
        @property
        def symbol(self):  # noqa: D401
            raise RuntimeError("boom")

    # _position_view swallows a bad position (returns None), so force the error
    # deeper by passing a non-iterable open_positions.
    a = gov.allocate(symbol="EURUSD", direction="LONG", open_positions=object())  # type: ignore[arg-type]
    assert a.approved is False
    assert "fail-closed" in a.reason


# ── 8. Legacy check() concentration API still works (backward compatible) ────


def test_legacy_check_still_returns_verdict():
    gov = PortfolioGovernor(_cfg())
    v = gov.check("EURUSD", "LONG", [], account_balance=10_000.0)
    assert v.allowed is True
    assert hasattr(v, "reason")


# ── 9. & 10. Live path: grading + ranking + allocation in _select_and_execute ─


def _system_with_governor(gov: PortfolioGovernor, orch: Orchestrator | None):
    sys = EventDrivenSystem.__new__(EventDrivenSystem)
    sys._ctx = SimpleNamespace(portfolio_governor=gov, orchestrator=orch)
    sys._consensus_entry_cooldown = {}
    sys._config = SimpleNamespace(
        orchestrator=SimpleNamespace(dimension_floor=0.6),
        consensus=SimpleNamespace(trigger_cooldown_seconds=300.0),
    )

    class _PM:
        def get_all_open_positions(self):
            return []

        def get_platform_balance(self, symbol):
            return 10_000.0

    sys._pm = _PM()
    captured: list = []
    sys._on_entry_decision = lambda d, allocation=None: captured.append((d, allocation))
    return sys, captured


def _item(direction: str, score: float, ev: float = 0.0, tf: str = "SWING"):
    cand = Candidate(direction=direction, timeframe_class=tf, score=score,
                     ev_estimate=ev)
    dec = {
        "symbol": "EURUSD", "direction": direction,
        "candidate_id": cand.candidate_id, "entry_price": 1.1000,
        "stop_loss": 1.0950, "tp1": 1.1100, "conviction": int(score * 100),
        "source": "consensus",
    }
    env = CandidateEntryDecision(symbol="EURUSD", candidate=cand, source="consensus")
    return (env, dec)


def test_select_and_execute_grades_and_funds_survivors():
    """grade_candidate runs in the live path: a uniformly weak candidate is
    dropped, the strong one is funded with its allocation budget."""
    gov = PortfolioGovernor(_cfg())
    orch = Orchestrator()
    sys, captured = _system_with_governor(gov, orch)

    good = _item("LONG", score=0.8, ev=1.0)
    weak = _item("LONG", score=0.05, ev=-1.0)  # grade < dimension floor → dropped
    sys._select_and_execute("EURUSD", [good, weak], sys._config.consensus)

    assert len(captured) == 1                       # weak idea grade-dropped
    dec, alloc = captured[0]
    assert dec["candidate_id"] == good[0].candidate.candidate_id
    assert isinstance(alloc, Allocation) and alloc.approved
    assert alloc.max_risk_pct == 2.0


def test_select_and_execute_symbol_cap_funds_best_first():
    """Rank-sorted candidates hit the caps best-first: with a 1-per-symbol cap
    the higher-grade idea is funded and the lower-grade one is denied."""
    gov = PortfolioGovernor(_cfg(max_positions_per_symbol=1))
    orch = Orchestrator()
    sys, captured = _system_with_governor(gov, orch)

    strong = _item("LONG", score=0.9, ev=1.2)
    weaker = _item("LONG", score=0.7, ev=0.4)
    # Provided lower-graded first to prove ranking (not input order) decides.
    sys._select_and_execute("EURUSD", [weaker, strong], sys._config.consensus)

    assert len(captured) == 1
    dec, _ = captured[0]
    assert dec["candidate_id"] == strong[0].candidate.candidate_id


def test_select_and_execute_legacy_lock_without_governor():
    """No PortfolioGovernor → legacy within-cycle direction lock (one direction,
    one-arg dispatch) so behaviour is unchanged without the allocator."""
    sys = EventDrivenSystem.__new__(EventDrivenSystem)
    sys._ctx = None
    sys._consensus_entry_cooldown = {}
    sys._config = SimpleNamespace(consensus=SimpleNamespace(trigger_cooldown_seconds=300.0))

    class _PM:
        def get_all_open_positions(self):
            return []

    sys._pm = _PM()
    captured: list = []
    sys._on_entry_decision = lambda d: captured.append(d)  # legacy one-arg

    items = [_item("LONG", 0.6), _item("SHORT", 0.9), _item("LONG", 0.7)]
    sys._select_and_execute("EURUSD", items, sys._config.consensus)

    # Direction lock: only the winning direction's candidates dispatch.
    assert len(captured) >= 1
    assert {d["direction"] for d in captured} == {"SHORT"}

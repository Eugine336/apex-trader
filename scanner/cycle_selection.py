"""APEX TRADER — Within-cycle entry-candidate selector (extracted from the bootstrap).

Phase K (Constitution Part XI — modular design): this pure cycle-boundary
selector was defined inline in the 11k-line ``event_driven_bootstrap.py``. It
ranks the ``(CandidateEntryDecision, decision_dict)`` tuples collected in one
analysis cycle best-first and applies the within-cycle direction lock. Lifting
it into a named, unit-tested module is part of the behaviour-preserving
decomposition; the bootstrap re-imports it so the existing
``from event_driven_bootstrap import select_cycle_candidates`` path resolves
identically — behaviour unchanged.
"""

from __future__ import annotations

import warnings


def select_cycle_candidates(items: list) -> tuple[list, list, str]:
    """Pure cycle-boundary selector for per-candidate entry decisions.

    ``items`` is a list of ``(CandidateEntryDecision, decision_dict)`` tuples
    collected from BOTH entry paths during one analysis cycle. Candidates are
    ranked best-first by their composite ``score`` (expected value as the
    stable tie-break), and the top candidate's direction wins this cycle:
    opposing-direction candidates are dropped so a single analysis cycle never
    submits both a LONG and a SHORT on the same symbol at once.

    Returns ``(survivors, dropped, winning_direction)``. ``survivors`` keeps the
    best-first order so the caller dispatches the strongest idea first.

    Session note: this within-cycle direction lock is intentionally simple. It
    is replaced in Session 3 by ``PortfolioGovernor.allocate`` which can fund
    opposing horizons (a LONG swing alongside a capped SHORT scalp) under a net
    exposure budget. Keeping the lock here prevents over-trading until those
    capital-allocation caps exist.
    """
    warnings.warn(
        "select_cycle_candidates is a retired legacy within-cycle selector, "
        "superseded by PortfolioGovernor.allocate; it is retained only for "
        "tests and is slated for removal — do not use it in new code.",
        DeprecationWarning,
        stacklevel=2,
    )
    if not items:
        return [], [], ""

    def _rank_key(item):
        cand = item[0].candidate
        return (
            float(getattr(cand, "score", 0.0) or 0.0),
            float(getattr(cand, "ev_estimate", 0.0) or 0.0),
        )

    ranked = sorted(items, key=_rank_key, reverse=True)
    winning_direction = str(getattr(ranked[0][0].candidate, "direction", "") or "")
    survivors = [
        it for it in ranked
        if str(getattr(it[0].candidate, "direction", "") or "") == winning_direction
    ]
    dropped = [
        it for it in ranked
        if str(getattr(it[0].candidate, "direction", "") or "") != winning_direction
    ]
    return survivors, dropped, winning_direction

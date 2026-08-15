"""APEX TRADER — Within-cycle entry-candidate RANKER (extracted from the bootstrap).

Phase K (Constitution Part XI — modular design): this pure cycle-boundary
selector was defined inline in the 11k-line ``event_driven_bootstrap.py``. It
ranks the ``(CandidateEntryDecision, decision_dict)`` tuples collected in one
analysis cycle best-first.

DIRECTIONAL_AUTHORITY (V-006): the original selector took the top candidate's
direction as the cycle "winning_direction" and dropped every opposing-direction
candidate. That imposed a within-cycle direction lock — a downstream module
deciding direction, which the constitution forbids. The market can hold a LONG
swing and a SHORT scalp at the same time, so that lock has been removed:
candidates now COEXIST across directions and are only ranked here. Funding or
capping competing horizons under a net-exposure budget is the job of
portfolio/risk reasoning (e.g. ``PortfolioGovernor.allocate``), not this module.

This module is dormant on the live path (the bootstrap no longer imports it) and
is retained only for backward-compatible imports and unit tests; calling it
emits a ``DeprecationWarning``.
"""

from __future__ import annotations

import warnings


def select_cycle_candidates(items: list) -> tuple[list, list, str]:
    """Rank per-candidate entry decisions best-first — no direction lock.

    ``items`` is a list of ``(CandidateEntryDecision, decision_dict)`` tuples
    collected from BOTH entry paths during one analysis cycle. Candidates are
    ranked best-first by their composite ``score`` (expected value as the stable
    tie-break) so the caller dispatches the strongest idea first.

    Constitution (DIRECTIONAL_AUTHORITY): opposing-direction candidates are NO
    LONGER dropped and no cycle "winning direction" is declared — direction is
    not this module's authority. Directions coexist here; portfolio/risk
    reasoning ranks and funds them under an exposure budget.

    Returns ``(survivors, dropped, winning_direction)`` for backward
    compatibility: ``survivors`` keeps the best-first order across ALL
    directions, ``dropped`` is always empty (nothing is dropped on direction),
    and ``winning_direction`` is always ``""`` (no direction is declared).
    """
    warnings.warn(
        "select_cycle_candidates is deprecated and dormant: it now only ranks "
        "candidates best-first and no longer imposes a within-cycle direction "
        "lock (directions coexist; portfolio/risk reasoning ranks them).",
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

    # Directions coexist: rank every candidate best-first, drop nothing on
    # direction, and declare no winning direction.
    survivors = sorted(items, key=_rank_key, reverse=True)
    return survivors, [], ""

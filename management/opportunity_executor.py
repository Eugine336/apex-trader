"""
APEX TRADER — Opportunity Executor

The ranker (`brain.opportunity_ranker`) turns the brain's module votes into a
ranked list of independent trade opportunities, each with its own expected
value.  The executor decides *which* of those opportunities deserves capital and
hands the chosen direction to the existing entry pipeline.

It deliberately does NOT replace the planner, the correlation engine, the
governor, or the risk stack.  It only answers the one question the scalar
``decide`` used to answer — "which direction?" — but now from a graded, ranked
view instead of a hand-raising sum.  Everything downstream (timing, sizing,
SKIP/WAIT, portfolio vetoes) still runs exactly as before on the selected
direction.

Two modes, controlled by ``OpportunityRankerConfig.execute``:
  * shadow  (execute=False) — select + log only; never drives a trade.
  * execute (execute=True)  — select the best opportunity and dispatch it.

Errors from the injected pipeline are logged loudly and surfaced — never
silently swallowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from loguru import logger

from brain.opportunity_ranker import Opportunity


@dataclass
class ExecutionOutcome:
    """Result of an executor pass over one instrument's candidates."""

    selected: list[Opportunity] = field(default_factory=list)
    executed: int = 0
    shadow: bool = False
    reason: str = ""
    # The ranked candidates that were NOT selected this pass (collapse #13).
    # Kept so the discarded tail is visible to the orchestrator / trace / dash
    # instead of silently vanishing after the top-N cut.
    alternatives: list[Opportunity] = field(default_factory=list)

    @property
    def any_executed(self) -> bool:
        return self.executed > 0


class OpportunityExecutor:
    """Selects ranked opportunities and (optionally) drives the entry pipeline.

    The pipeline is injected as a callable so the executor stays decoupled from
    the main loop and is trivially testable.  The callable receives a single
    ``Opportunity`` and returns ``True`` when a trade was actually placed.
    """

    def __init__(self, config) -> None:
        # Duck-typed OpportunityRankerConfig — read defensively so a missing
        # field never crashes the live loop.
        self.config = config

    # ── Selection ─────────────────────────────────────────────────────────

    def select(
        self,
        candidates: Optional[list[Opportunity]],
        scorer: Optional[Callable[[Opportunity], float]] = None,
    ) -> Optional[Opportunity]:
        """Return the single best opportunity, or ``None`` when there is none.

        Candidates are assumed pre-ranked (best-first) by the ranker. With no
        ``scorer`` this just returns the ranker's top idea (legacy behaviour). A
        ``scorer`` (e.g. the orchestrator's per-candidate grader) lets the round
        table choose among ALL candidates by their graded evidence instead of
        blindly taking ``candidates[0]`` — so the most defensible idea wins, not
        merely the top of the raw EV sort. Ties keep the higher-ranked (earlier)
        candidate since they are iterated in rank order with a strict ``>``.
        """
        if not candidates:
            return None
        if scorer is None:
            return candidates[0]
        best = candidates[0]
        best_score: Optional[float] = None
        for c in candidates:
            try:
                s = float(scorer(c))
            except Exception as exc:
                logger.debug("[executor] candidate scorer failed for {}: {}",
                             getattr(c, "summary", c), exc)
                continue
            if best_score is None or s > best_score:
                best_score, best = s, c
        return best

    def select_top(
        self,
        candidates: Optional[list[Opportunity]],
        max_concurrent: Optional[int] = None,
    ) -> list[Opportunity]:
        """Return up to ``max_concurrent`` best opportunities.

        ``max_concurrent`` defaults to the config value but can be overridden by
        the caller (e.g. capacity-aware dispatch) so the cut tracks real free
        trade slots instead of a fixed config constant (collapse #13).
        """
        if not candidates:
            return []
        if max_concurrent is None:
            max_concurrent = int(getattr(self.config, "max_concurrent", 1) or 1)
        max_concurrent = max(0, int(max_concurrent))
        return list(candidates[:max_concurrent])

    # ── Execution ─────────────────────────────────────────────────────────

    def execute(
        self,
        candidates: Optional[list[Opportunity]],
        dispatch: Callable[[Opportunity], bool],
        *,
        label: str = "",
        max_concurrent: Optional[int] = None,
    ) -> ExecutionOutcome:
        """Drive the injected pipeline for the selected opportunities.

        In shadow mode the candidates are logged and returned without ever
        calling ``dispatch``.  In execute mode each selected opportunity is
        dispatched in rank order until one is placed or capacity is exhausted.
        ``max_concurrent`` overrides the config cap (capacity-aware dispatch).
        The un-selected ranked tail is preserved on ``alternatives`` rather than
        silently discarded (collapse #13).
        """
        execute_live = bool(getattr(self.config, "execute", False))
        chosen = self.select_top(candidates, max_concurrent=max_concurrent)
        chosen_ids = {id(c) for c in chosen}
        alternatives = [c for c in (candidates or []) if id(c) not in chosen_ids]

        if not chosen:
            return ExecutionOutcome(selected=[], executed=0, shadow=not execute_live,
                                    reason="no candidates", alternatives=alternatives)

        if not execute_live:
            logger.info(
                "[executor] {} SHADOW — {} candidate(s), best: {}",
                label or "pair",
                len(chosen),
                chosen[0].summary,
            )
            return ExecutionOutcome(
                selected=chosen, executed=0, shadow=True,
                reason="shadow mode (execute disabled)",
                alternatives=alternatives,
            )

        executed = 0
        for opp in chosen:
            try:
                placed = dispatch(opp)
            except Exception as exc:
                # Loud, surfaced — a pipeline failure must never be silent.
                logger.error(
                    "[executor] {} dispatch FAILED for {}: {}",
                    label or "pair", opp.summary, exc,
                )
                raise
            if placed:
                executed += 1
                logger.info(
                    "[executor] {} EXECUTED {} ({}/{})",
                    label or "pair", opp.summary, executed, len(chosen),
                )
            else:
                logger.info(
                    "[executor] {} declined downstream — {}",
                    label or "pair", opp.summary,
                )

        return ExecutionOutcome(
            selected=chosen, executed=executed, shadow=False,
            reason=f"executed {executed}/{len(chosen)}",
            alternatives=alternatives,
        )

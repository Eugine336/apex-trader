"""
APEX TRADER — Opportunity Executor (management layer)

Consumes the ranked opportunities produced by the intelligence layer
(:func:`brain.opportunity_ranker.rank_opportunities`) and decides which one(s)
deserve capital, then drives each selected idea through the *existing* entry
pipeline (OQ → EQ → EntryEngine → DecisionEngine → risk stack).

The pipeline is injected as a callable so this module stays decoupled from the
heavy decision/broker machinery and is unit-testable without torch/pandas.  The
live ``main_loop`` supplies the real pipeline; tests supply a stub.

Failures are never swallowed: a pipeline raising is logged at ERROR and recorded
on the outcome, and execution continues with the next ranked idea so one bad
candidate cannot silently abort the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from loguru import logger

from brain.opportunity_ranker import Opportunity

# A pipeline takes a selected Opportunity and attempts to act on it, returning a
# truthy domain object on success (e.g. an EntryDecision) or None on a clean
# skip.  Raising is allowed — the executor catches, logs loudly, and records it.
Pipeline = Callable[[Opportunity], object]


@dataclass
class ExecutionOutcome:
    """The result of attempting (or shadowing) one ranked opportunity."""

    opportunity: Opportunity
    allocated: bool                 # did this idea receive capital intent?
    dispatched: bool                # did the pipeline actually run (execute on)?
    result: object = None           # pipeline return value, if any
    skipped_reason: str = ""        # why it was not allocated/dispatched
    error: Optional[BaseException] = None

    @property
    def summary(self) -> str:
        if self.error is not None:
            tail = f"ERROR {type(self.error).__name__}: {self.error}"
        elif not self.allocated:
            tail = f"skipped ({self.skipped_reason})"
        elif not self.dispatched:
            tail = "shadow (execute disabled)"
        else:
            tail = "dispatched"
        return f"{self.opportunity.summary} → {tail}"


class OpportunityExecutor:
    """Allocates capital across ranked opportunities and drives the pipeline.

    Parameters
    ----------
    max_concurrent:
        Maximum number of top-ranked opportunities to act on per call.
    execute:
        When False (default for safety), the executor produces shadow outcomes
        — it ranks and selects but never dispatches, so the live path is
        unaffected while data is gathered.
    """

    def __init__(self, max_concurrent: int = 1, execute: bool = False) -> None:
        if max_concurrent < 1:
            raise ValueError(
                f"max_concurrent must be >= 1, got {max_concurrent!r}"
            )
        self.max_concurrent = max_concurrent
        self.execute = execute

    @classmethod
    def from_config(cls, ranker_cfg) -> "OpportunityExecutor":
        """Build from an ``OpportunityRankerConfig``."""
        return cls(
            max_concurrent=ranker_cfg.max_concurrent_opportunities,
            execute=ranker_cfg.execute,
        )

    def execute_opportunities(
        self,
        opportunities: list[Opportunity],
        pipeline: Pipeline,
    ) -> list[ExecutionOutcome]:
        """Select the best ranked ideas and run each through ``pipeline``.

        ``opportunities`` is expected pre-ranked (best first) but is defensively
        re-sorted by composite score.  Returns one outcome per opportunity
        considered, in the order considered.
        """
        if not opportunities:
            logger.info("[executor] no opportunities to execute")
            return []

        ordered = sorted(opportunities, key=lambda o: o.score, reverse=True)
        outcomes: list[ExecutionOutcome] = []
        allocated = 0

        for opp in ordered:
            if allocated >= self.max_concurrent:
                outcomes.append(
                    ExecutionOutcome(
                        opportunity=opp,
                        allocated=False,
                        dispatched=False,
                        skipped_reason=(
                            f"capacity reached ({self.max_concurrent})"
                        ),
                    )
                )
                continue

            allocated += 1

            if not self.execute:
                logger.info("[executor] shadow — would dispatch {}", opp.summary)
                outcomes.append(
                    ExecutionOutcome(
                        opportunity=opp,
                        allocated=True,
                        dispatched=False,
                        skipped_reason="execute disabled",
                    )
                )
                continue

            try:
                result = pipeline(opp)
            except Exception as exc:  # surface loudly, never swallow
                logger.error(
                    "[executor] pipeline raised for {}: {}", opp.summary, exc
                )
                outcomes.append(
                    ExecutionOutcome(
                        opportunity=opp,
                        allocated=True,
                        dispatched=True,
                        error=exc,
                    )
                )
                continue

            logger.info("[executor] dispatched {} → {!r}", opp.summary, result)
            outcomes.append(
                ExecutionOutcome(
                    opportunity=opp,
                    allocated=True,
                    dispatched=True,
                    result=result,
                )
            )

        return outcomes

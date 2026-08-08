"""
APEX TRADER — Cross-Instrument Opportunity Ranker (GAP 2)

The per-instrument ranker (:mod:`brain.opportunity_ranker`) ranks ideas WITHIN
one instrument. Nothing compares EURUSD EV=0.8R against GBPJPY EV=1.2R. This
module closes that gap: given the candidates the global opportunity queue
collected across every instrument during one window, it normalises their EVs for
fair cross-instrument comparison and returns a single best-first ranking.

Normalisation applied to each candidate's raw EV (all in R units):
  * spread cost     — a wider spread eats edge, so EV is docked
    ``spread_ev_penalty_per_pip × spread_pips``.
  * per-pair skill  — a calibrated per-instrument win rate (from the pair
    learner, if wired) nudges EV by ``winrate_ev_weight × (win_rate − 0.5)``.

It is a PURE ranking helper: it never opens a trade, never touches the broker,
never mutates anything except writing the computed ``adjusted_ev`` back onto each
candidate (a documented, in-place annotation the queue dispatches on). Inputs are
duck-typed so any object exposing ``symbol`` / ``ev`` works — there is no import
of the queue type, so there is no import cycle.

Leaf module: stdlib + loguru only.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from loguru import logger


class CrossInstrumentRanker:
    """Normalise and rank opportunities across instruments (best EV first)."""

    def __init__(
        self,
        *,
        spread_pips_lookup: Optional[Callable[[str], float]] = None,
        win_rate_lookup: Optional[Callable[[str], Optional[float]]] = None,
        spread_ev_penalty_per_pip: float = 0.02,
        winrate_ev_weight: float = 0.5,
    ) -> None:
        # Both lookups are best-effort; a missing/raising hook leaves that
        # adjustment neutral so a bad provider can never break ranking.
        self._spread = spread_pips_lookup
        self._win_rate = win_rate_lookup
        self._pip_penalty = max(0.0, float(spread_ev_penalty_per_pip))
        self._wr_weight = float(winrate_ev_weight)

    def bind_lookups(
        self,
        *,
        spread_pips_lookup: Optional[Callable[[str], float]] = None,
        win_rate_lookup: Optional[Callable[[str], Optional[float]]] = None,
    ) -> None:
        """Attach runtime per-instrument lookups (spread / win rate).

        Lets the pure ranker be constructed early (e.g. in SystemContext) and
        wired to the broker/learning callables later by the event-driven system.
        A ``None`` argument leaves that lookup unchanged.
        """
        if spread_pips_lookup is not None:
            self._spread = spread_pips_lookup
        if win_rate_lookup is not None:
            self._win_rate = win_rate_lookup

    # ── Per-candidate adjustment ──────────────────────────────────────────

    def adjusted_ev(self, candidate: Any) -> float:
        """Cross-instrument-comparable EV for one candidate (R units)."""
        symbol = str(getattr(candidate, "symbol", "") or "")
        ev = float(getattr(candidate, "ev", 0.0) or 0.0)

        spread_pips = 0.0
        if self._spread is not None and symbol:
            try:
                spread_pips = max(0.0, float(self._spread(symbol)))
            except Exception as exc:  # noqa: BLE001 — never break ranking
                logger.debug("[xrank] spread lookup failed for {}: {}", symbol, exc)
                spread_pips = 0.0

        win_adjust = 0.0
        if self._win_rate is not None and symbol:
            try:
                wr = self._win_rate(symbol)
                if wr is not None:
                    win_adjust = self._wr_weight * (float(wr) - 0.5)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[xrank] win-rate lookup failed for {}: {}", symbol, exc)
                win_adjust = 0.0

        return ev - self._pip_penalty * spread_pips + win_adjust

    # ── Ranking ───────────────────────────────────────────────────────────

    def rank(self, candidates: Sequence[Any]) -> list[Any]:
        """Return candidates best-first by cross-instrument-adjusted EV.

        Each candidate's ``adjusted_ev`` attribute is set as a side effect so the
        queue (and the dashboard) can see WHY one instrument outranked another.
        Ties break on raw ``ev`` then ``confidence`` for a stable order. An empty
        or single-element input is returned unchanged (sorted is a no-op).
        """
        items = list(candidates or [])
        for c in items:
            try:
                c.adjusted_ev = self.adjusted_ev(c)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[xrank] adjust failed, using raw ev: {}", exc)
                try:
                    c.adjusted_ev = float(getattr(c, "ev", 0.0) or 0.0)
                except Exception:
                    c.adjusted_ev = 0.0

        items.sort(
            key=lambda c: (
                float(getattr(c, "adjusted_ev", 0.0) or 0.0),
                float(getattr(c, "ev", 0.0) or 0.0),
                float(getattr(c, "confidence", 0.0) or 0.0),
            ),
            reverse=True,
        )
        if items:
            logger.debug(
                "[xrank] ranked {} candidate(s) across instruments: {}",
                len(items),
                " | ".join(
                    f"{getattr(c, 'symbol', '?')}:{getattr(c, 'direction', '?')}"
                    f"={float(getattr(c, 'adjusted_ev', 0.0) or 0.0):+.2f}R"
                    for c in items
                ),
            )
        return items


__all__ = ["CrossInstrumentRanker"]

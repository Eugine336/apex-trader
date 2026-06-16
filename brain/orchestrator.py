"""
APEX TRADER — Trade Orchestrator (the round table)

The entry pipeline historically behaved as a *serial kill-chain*: each gate
(scanner READY floors, decision-engine enter/skip, planner agreement, regime
threshold, …) collapsed its rich, multi-dimensional read into a single number,
compared it against a hard threshold, and *killed* the setup when it fell short.
Information was destroyed at every step and a single weak dimension could veto an
otherwise-excellent trade.

The Orchestrator is the opposite stance — a round table where every component's
evidence is *collected*, *kept whole*, and turned into a graded answer to the
only question that should remain open-ended:

    not "should I trade this?" (yes/no)  →  but "how big should I trade this?"

It reads the evidence every stage already produced (the ranker's candidates and
their EV/coherence, the situation engine's per-timeframe alignment vector, the
decision engine's continuous enter/skip margin and conviction, the planner's
per-advisor vector, the scanner score) and folds each dimension into a *bounded
size multiplier* in ``[size_floor, 1.0]``.  Weak dimensions *dim* the size; they
do not extinguish the trade.

The ONLY hard vetoes are physics — constraints the broker/account enforce and no
amount of conviction can overrule (negative free margin, market closed, a
duplicate position already open, below the broker's minimum lot).  Everything
analytical is a dimmer, never a switch.

Design rules:
  * Pure + side-effect free maths — no broker, network, pandas or torch here.
  * Every dimension records *why* it contributed what it did (for the trace and
    the dashboard) — nothing is silently ignored.
  * Backward compatible: the caller chooses whether the multiplier actually
    drives live sizing.  When it does, the multiplier is bounded so it can only
    SIZE DOWN a trade the existing risk gates already approved — never up, never
    to zero (physics vetoes aside).  It therefore cannot place a trade the old
    path would have refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


# ── Physics vetoes — the only hard "no" the orchestrator honours ───────────
# These are real-world constraints the account/broker enforce; conviction can
# never overrule them.  Anything else is a size dimmer, not a kill switch.
VETO_NEGATIVE_MARGIN = "negative_free_margin"
VETO_MARKET_CLOSED = "market_closed"
VETO_DUPLICATE_POSITION = "duplicate_position"
VETO_BELOW_MIN_LOT = "below_broker_min_lot"

PHYSICS_VETOES: tuple[str, ...] = (
    VETO_NEGATIVE_MARGIN,
    VETO_MARKET_CLOSED,
    VETO_DUPLICATE_POSITION,
    VETO_BELOW_MIN_LOT,
)


@dataclass
class DimensionContribution:
    """One evidence dimension's contribution to the graded size.

    ``multiplier`` is the bounded factor (``[dim_floor, 1.0]``) this dimension
    applied; ``raw`` is the underlying value it was derived from; ``reason`` is
    the human-readable justification surfaced on the trace / dashboard.
    """

    name: str
    multiplier: float
    raw: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "multiplier": round(self.multiplier, 4),
            "raw": round(self.raw, 4),
            "reason": self.reason,
        }


@dataclass
class TradeProposal:
    """All the evidence one candidate trade carries into the round table.

    Every field is optional / defensively defaulted: a missing dimension simply
    contributes a neutral multiplier rather than crashing or silently vetoing.
    The orchestrator never mutates this object.
    """

    pair: str
    direction: str                      # "LONG" | "SHORT"
    horizon: str = ""                   # "SCALP" | "SWING" | "" (scalar fallback)

    # ── Ranker evidence ─────────────────────────────────────────────────
    ranker_ev: Optional[float] = None       # selected opportunity EV in R units
    ranker_coherence: Optional[float] = None  # 0..1 cluster dominance
    ranker_confidence: Optional[float] = None  # 0..1 weighted cluster confidence
    candidate_count: int = 0

    # ── Situation engine evidence ────────────────────────────────────────
    tf_alignment: Optional[float] = None    # -1..+1 signed HTF support
    tf_vector: dict = field(default_factory=dict)  # {"D1": (dir, conf), ...}

    # ── Decision engine evidence ─────────────────────────────────────────
    de_margin: Optional[float] = None       # continuous enter-skip margin
    de_conviction: Optional[float] = None   # 0..1 conviction

    # ── Planner evidence ─────────────────────────────────────────────────
    advisor_agreement: Optional[float] = None   # 0..1 mean agreement
    advisor_vector: dict = field(default_factory=dict)  # per-advisor signed align

    # ── Scanner evidence ─────────────────────────────────────────────────
    scan_score: Optional[float] = None      # 0..~123 confluence score

    # ── Physics flags (the only hard vetoes) ─────────────────────────────
    # A truthy flag here means the trade is physically impossible right now.
    physics_vetoes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "direction": self.direction,
            "horizon": self.horizon,
            "ranker_ev": self.ranker_ev,
            "ranker_coherence": self.ranker_coherence,
            "ranker_confidence": self.ranker_confidence,
            "candidate_count": self.candidate_count,
            "tf_alignment": self.tf_alignment,
            "tf_vector": {k: list(v) for k, v in self.tf_vector.items()},
            "de_margin": self.de_margin,
            "de_conviction": self.de_conviction,
            "advisor_agreement": self.advisor_agreement,
            "advisor_vector": self.advisor_vector,
            "scan_score": self.scan_score,
            "physics_vetoes": list(self.physics_vetoes),
        }


@dataclass
class OrchestratorVerdict:
    """The round table's graded answer for one proposal."""

    pair: str
    direction: str
    horizon: str
    size_multiplier: float                  # bounded [size_floor, 1.0] (0.0 if vetoed)
    vetoed: bool
    veto_reason: str
    dimensions: list[DimensionContribution] = field(default_factory=list)

    @property
    def tradeable(self) -> bool:
        return not self.vetoed and self.size_multiplier > 0.0

    def summary(self) -> str:
        if self.vetoed:
            return f"{self.pair} {self.direction} VETOED ({self.veto_reason})"
        dims = ", ".join(f"{d.name}×{d.multiplier:.2f}" for d in self.dimensions)
        return (
            f"{self.pair} {self.direction} {self.horizon or 'full-HTF'} "
            f"size×{self.size_multiplier:.2f} [{dims}]"
        )

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "direction": self.direction,
            "horizon": self.horizon,
            "size_multiplier": round(self.size_multiplier, 4),
            "vetoed": self.vetoed,
            "veto_reason": self.veto_reason,
            "dimensions": [d.to_dict() for d in self.dimensions],
        }


class Orchestrator:
    """Folds a :class:`TradeProposal`'s evidence into a graded size multiplier.

    The config is duck-typed (an ``OrchestratorConfig``) and read defensively so
    a missing field never crashes the live loop.  ``evaluate`` is a pure function
    of the proposal + config — trivially unit-testable.
    """

    def __init__(self, config=None) -> None:
        self.config = config

    # ── Config accessors (defensive) ─────────────────────────────────────
    def _cfg(self, name: str, default):
        return getattr(self.config, name, default) if self.config is not None else default

    @staticmethod
    def _bound(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    # ── Per-dimension dimmers ────────────────────────────────────────────
    # Each returns a multiplier in [dim_floor, 1.0]; a missing input returns a
    # neutral 1.0 so an absent dimension neither helps nor kills the trade.

    def _dim_floor(self) -> float:
        return float(self._cfg("dimension_floor", 0.6))

    def _ranker_ev_dim(self, ev: Optional[float]) -> DimensionContribution:
        floor = self._dim_floor()
        if ev is None:
            return DimensionContribution("ranker_ev", 1.0, 0.0, "no ranker EV (neutral)")
        ev_full = float(self._cfg("ranker_ev_full", 1.5))
        # EV<=0 → floor; EV>=ev_full → 1.0; linear between.
        if ev_full <= 0:
            mult = 1.0
        else:
            mult = self._bound(floor + (1.0 - floor) * (ev / ev_full), floor, 1.0)
        return DimensionContribution(
            "ranker_ev", mult, ev, f"EV {ev:+.2f}R vs full {ev_full:.2f}R"
        )

    def _coherence_dim(self, coherence: Optional[float]) -> DimensionContribution:
        floor = self._dim_floor()
        if coherence is None:
            return DimensionContribution("coherence", 1.0, 0.0, "no coherence (neutral)")
        mult = self._bound(floor + (1.0 - floor) * coherence, floor, 1.0)
        return DimensionContribution(
            "coherence", mult, coherence, f"cluster coherence {coherence:.0%}"
        )

    def _tf_alignment_dim(self, tf_alignment: Optional[float], horizon: str) -> DimensionContribution:
        """HTF alignment as *bounded context*, never a veto.

        Positive alignment → full size.  Negative alignment dims size toward the
        floor — but only partially, and the dimming is itself softened for a
        SCALP horizon (the ranker already chose a fast idea that legitimately
        trades against a slower timeframe).
        """
        floor = self._dim_floor()
        if tf_alignment is None:
            return DimensionContribution("tf_alignment", 1.0, 0.0, "no HTF read (neutral)")
        if tf_alignment >= 0:
            return DimensionContribution(
                "tf_alignment", 1.0, tf_alignment,
                f"HTF supports ({tf_alignment:+.2f})",
            )
        # Opposing HTF: dim toward floor. SCALP softens the penalty (HTF demoted).
        opposition = min(1.0, abs(tf_alignment))
        if str(horizon).upper() == "SCALP":
            opposition *= float(self._cfg("scalp_htf_opposition_scale", 0.3))
        mult = self._bound(1.0 - (1.0 - floor) * opposition, floor, 1.0)
        return DimensionContribution(
            "tf_alignment", mult, tf_alignment,
            f"HTF opposes ({tf_alignment:+.2f}) — bounded dim ({horizon or 'full-HTF'})",
        )

    def _de_margin_dim(self, margin: Optional[float]) -> DimensionContribution:
        floor = self._dim_floor()
        if margin is None:
            return DimensionContribution("de_margin", 1.0, 0.0, "no DE margin (neutral)")
        margin_full = float(self._cfg("de_margin_full", 0.5))
        if margin_full <= 0:
            mult = 1.0
        else:
            mult = self._bound(floor + (1.0 - floor) * (margin / margin_full), floor, 1.0)
        return DimensionContribution(
            "de_margin", mult, margin, f"DE enter-skip margin {margin:+.2f}"
        )

    def _conviction_dim(self, conviction: Optional[float]) -> DimensionContribution:
        floor = self._dim_floor()
        if conviction is None:
            return DimensionContribution("conviction", 1.0, 0.0, "no conviction (neutral)")
        mult = self._bound(floor + (1.0 - floor) * conviction, floor, 1.0)
        return DimensionContribution(
            "conviction", mult, conviction, f"DE conviction {conviction:.2f}"
        )

    def _advisor_dim(self, agreement: Optional[float]) -> DimensionContribution:
        floor = self._dim_floor()
        if agreement is None:
            return DimensionContribution("advisor_agreement", 1.0, 0.0, "no advisors (neutral)")
        mult = self._bound(floor + (1.0 - floor) * agreement, floor, 1.0)
        return DimensionContribution(
            "advisor_agreement", mult, agreement, f"planner advisor agreement {agreement:.2f}"
        )

    def _scan_score_dim(self, score: Optional[float]) -> DimensionContribution:
        floor = self._dim_floor()
        if score is None:
            return DimensionContribution("scan_score", 1.0, 0.0, "no scan score (neutral)")
        score_full = float(self._cfg("scan_score_full", 100.0))
        if score_full <= 0:
            mult = 1.0
        else:
            mult = self._bound(floor + (1.0 - floor) * (score / score_full), floor, 1.0)
        return DimensionContribution(
            "scan_score", mult, score, f"scan score {score:.0f} vs full {score_full:.0f}"
        )

    # ── Main entry point ─────────────────────────────────────────────────
    def evaluate(self, proposal: TradeProposal) -> OrchestratorVerdict:
        """Grade one proposal into a bounded size multiplier (or a physics veto)."""
        # Physics first — the only hard "no".
        physics = [v for v in (proposal.physics_vetoes or []) if v]
        if physics:
            return OrchestratorVerdict(
                pair=proposal.pair,
                direction=proposal.direction,
                horizon=proposal.horizon,
                size_multiplier=0.0,
                vetoed=True,
                veto_reason=", ".join(physics),
                dimensions=[],
            )

        dims = [
            self._ranker_ev_dim(proposal.ranker_ev),
            self._coherence_dim(proposal.ranker_coherence),
            self._tf_alignment_dim(proposal.tf_alignment, proposal.horizon),
            self._de_margin_dim(proposal.de_margin),
            self._conviction_dim(proposal.de_conviction),
            self._advisor_dim(proposal.advisor_agreement),
            self._scan_score_dim(proposal.scan_score),
        ]

        product = 1.0
        for d in dims:
            product *= d.multiplier

        size_floor = float(self._cfg("size_floor", 0.5))
        size_mult = self._bound(product, size_floor, 1.0)

        verdict = OrchestratorVerdict(
            pair=proposal.pair,
            direction=proposal.direction,
            horizon=proposal.horizon,
            size_multiplier=round(size_mult, 4),
            vetoed=False,
            veto_reason="",
            dimensions=dims,
        )
        logger.debug("[orchestrator] {}", verdict.summary())
        return verdict

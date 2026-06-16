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

import time
from dataclasses import dataclass, field
from enum import Enum
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


def gate_quality_multiplier(
    measures: list[tuple[float, float]], floor: float = 0.15
) -> float:
    """Bounded quality multiplier for a *softened* upstream kill-gate.

    The serial pipeline historically *killed* any setup that fell short of a
    hard quality threshold (scanner OQ/EQ/score, planner conviction, entry
    score). With the orchestrator as the live sizer those gates can instead
    *dim* the trade: a near-miss flows through small, a far-miss flows through
    tiny, only a truly hopeless setup (caught by a separate hard safety floor)
    still dies.

    ``measures`` is a list of ``(value, threshold)`` pairs. A dimension at or
    above its threshold contributes ``1.0`` (no penalty); a dimension below
    contributes ``value / threshold`` (<1.0). The result is the product of the
    per-dimension ratios, bounded to ``[floor, 1.0]`` — so being short on two
    dimensions dims more than being short on one, and the multiplier is never
    zero (a graded "barely" is still a tiny trade) nor above 1.0 (a gate can
    only size DOWN, never up). Pure function — trivially unit-testable.
    """
    mult = 1.0
    for value, threshold in measures:
        try:
            threshold = float(threshold)
            value = float(value)
        except (TypeError, ValueError):
            continue
        if threshold <= 0:
            continue
        ratio = value / threshold
        if ratio < 1.0:
            mult *= max(0.0, ratio)
    return max(floor, min(1.0, mult))


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
    # #6 — when the DE enter/skip gate was *softened* (margin non-positive but
    # above the hard safety floor), this carries the bounded quality multiplier
    # the engine derived from how negative the margin was. ``None`` (the default)
    # means the gate was not softened → neutral, no extra dimming. The existing
    # ``de_margin`` dimension saturates at its floor for *any* negative margin,
    # so this dimension preserves the gradient (−0.01 vs −0.9) that would
    # otherwise be lost between the engine and the round table.
    de_quality_multiplier: Optional[float] = None

    # ── Planner evidence ─────────────────────────────────────────────────
    advisor_agreement: Optional[float] = None   # 0..1 mean agreement
    advisor_vector: dict = field(default_factory=dict)  # per-advisor signed align

    # ── Scanner evidence ─────────────────────────────────────────────────
    scan_score: Optional[float] = None      # 0..~123 confluence score

    # ── Upstream gate-softening multipliers (Phase 9) ─────────────────────
    # Each is a bounded [gate_floor, 1.0] factor a softened upstream QUALITY
    # gate handed through instead of killing the setup: how far below the kill
    # threshold it was. 1.0 = the gate passed cleanly (no penalty). The
    # orchestrator folds these into the final size so a near-miss trades SMALL.
    gate_quality_multiplier: float = 1.0      # scanner READY + revalidation
    planner_quality_multiplier: float = 1.0   # planner conviction floor
    entry_quality_multiplier: float = 1.0     # entry-score floor

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
            "de_quality_multiplier": self.de_quality_multiplier,
            "advisor_agreement": self.advisor_agreement,
            "advisor_vector": self.advisor_vector,
            "scan_score": self.scan_score,
            "gate_quality_multiplier": self.gate_quality_multiplier,
            "planner_quality_multiplier": self.planner_quality_multiplier,
            "entry_quality_multiplier": self.entry_quality_multiplier,
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


# ── Live-position management types (round table for OPEN trades) ───────────
class ManagementAction(Enum):
    """Graded management verdict for an open position, ordered by escalation.

    A continuous health score maps to one of these — replacing the old argmax
    collapse over 4 action scores.  Every action except SCALE_UP is a one-way
    *de-risk* (hold, tighten, trim, exit); SCALE_UP is opt-in and only fires when
    the position is demonstrably healthier than at entry.
    """

    HOLD = "HOLD"
    TIGHTEN_SL = "TIGHTEN_SL"
    SCALE_DOWN = "SCALE_DOWN"
    EXIT_PARTIAL = "EXIT_PARTIAL"
    EXIT_FULL = "EXIT_FULL"
    SCALE_UP = "SCALE_UP"


@dataclass
class PositionEvidence:
    """The current, re-evaluated evidence for one OPEN position.

    Mirrors :class:`TradeProposal` but for the management round table: every
    field is optionally defaulted so a missing dimension contributes a neutral
    multiplier rather than crashing.  The orchestrator never mutates this object.
    ``entry_*`` fields carry the snapshot captured when the trade was opened so
    health can compare *then vs now*.
    """

    pair: str
    direction: str
    horizon: str = ""

    # ── Current situation read (from the situation engine) ────────────────
    profit_r: Optional[float] = None
    momentum: Optional[float] = None          # -1..+1 signed (with/against trade)
    structure_integrity: Optional[float] = None  # 0..1
    tf_alignment: Optional[float] = None      # -1..+1 signed HTF support
    tf_vector: dict = field(default_factory=dict)
    read_confidence: Optional[float] = None
    urgency: Optional[float] = None

    # ── Timing ────────────────────────────────────────────────────────────
    hold_minutes: float = 0.0
    expected_hold_minutes: float = 0.0

    # ── Account / portfolio exposure ──────────────────────────────────────
    portfolio_heat_pct: float = 0.0

    # ── Entry baseline snapshot (for thesis / situation-shift comparison) ──
    entry_health: Optional[float] = None
    entry_structure_integrity: Optional[float] = None
    entry_tf_alignment: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "direction": self.direction,
            "horizon": self.horizon,
            "profit_r": self.profit_r,
            "momentum": self.momentum,
            "structure_integrity": self.structure_integrity,
            "tf_alignment": self.tf_alignment,
            "tf_vector": {k: list(v) if isinstance(v, (list, tuple)) else v
                          for k, v in self.tf_vector.items()},
            "read_confidence": self.read_confidence,
            "urgency": self.urgency,
            "hold_minutes": self.hold_minutes,
            "expected_hold_minutes": self.expected_hold_minutes,
            "portfolio_heat_pct": self.portfolio_heat_pct,
            "entry_health": self.entry_health,
            "entry_structure_integrity": self.entry_structure_integrity,
            "entry_tf_alignment": self.entry_tf_alignment,
        }


@dataclass
class PositionHealthReport:
    """The round table's graded health verdict for one open position."""

    pair: str
    direction: str
    horizon: str
    health_score: float                       # 0..1 (product of dimension healths)
    action: ManagementAction
    dimension_scores: dict = field(default_factory=dict)   # name → multiplier
    dimensions: list[DimensionContribution] = field(default_factory=list)
    entry_health_at_open: Optional[float] = None
    health_delta: float = 0.0
    thesis_changes: list[str] = field(default_factory=list)
    recommended_sl: Optional[float] = None
    recommended_size_pct: Optional[float] = None   # fraction to CLOSE for trims
    timestamp: float = field(default_factory=time.time)

    def summary(self) -> str:
        dims = ", ".join(f"{d.name}×{d.multiplier:.2f}" for d in self.dimensions)
        return (
            f"{self.pair} {self.direction} {self.horizon or 'full-HTF'} "
            f"health={self.health_score:.2f} → {self.action.value} "
            f"(Δ{self.health_delta:+.2f}) [{dims}]"
        )

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "direction": self.direction,
            "horizon": self.horizon,
            "health_score": round(self.health_score, 4),
            "action": self.action.value,
            "dimension_scores": self.dimension_scores,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "entry_health_at_open": self.entry_health_at_open,
            "health_delta": round(self.health_delta, 4),
            "thesis_changes": list(self.thesis_changes),
            "recommended_sl": self.recommended_sl,
            "recommended_size_pct": self.recommended_size_pct,
            "timestamp": self.timestamp,
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

    def _de_quality_dim(self, quality: Optional[float]) -> DimensionContribution:
        """Softened DE gate (#6) quality gradient.

        Neutral (1.0) when the gate was not softened — the common case. When the
        decision engine let a *mildly* negative enter/skip margin flow through
        instead of killing it, ``quality`` is the bounded multiplier it derived
        from how negative the margin was (−0.01 → ~0.99, −0.5 → 0.5). Folding it
        in here is the single place the softened-DE dimming is applied, and it
        preserves the negative-margin gradient that ``_de_margin_dim`` saturates
        away at its floor.
        """
        if quality is None:
            return DimensionContribution(
                "de_quality", 1.0, 1.0, "DE gate not softened (neutral)"
            )
        mult = self._bound(float(quality), 0.0, 1.0)
        return DimensionContribution(
            "de_quality", mult, quality,
            f"DE gate softened — quality ×{mult:.2f}",
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

    # ── Candidate selection (round table chooses among ranked ideas) ──────
    def grade_candidate(
        self,
        *,
        direction: str = "",
        horizon: str = "",
        ranker_ev: Optional[float] = None,
        ranker_coherence: Optional[float] = None,
        ranker_confidence: Optional[float] = None,
    ) -> float:
        """Graded standalone score for ONE ranked candidate (pure, [floor, 1.0]).

        The executor historically dispatched ``candidates[0]`` — whatever the
        ranker's raw EV sort put first. That re-collapses the very information the
        ranker preserved (a marginally higher-EV but low-coherence idea would win
        over a slightly-lower-EV but far more coherent one). This lets the round
        table grade each candidate from its own evidence (EV × coherence ×
        confidence) so the *most defensible* idea — not just the top of the EV
        sort — can be chosen. Uses only the candidate's standalone ranker fields
        (no downstream pipeline evidence, which is direction-specific and not yet
        computed at selection time).
        """
        product = 1.0
        product *= self._ranker_ev_dim(ranker_ev).multiplier
        product *= self._coherence_dim(ranker_coherence).multiplier
        if ranker_confidence is not None:
            floor = self._dim_floor()
            product *= self._bound(
                floor + (1.0 - floor) * float(ranker_confidence), floor, 1.0
            )
        return self._bound(product, 0.0, 1.0)

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
            self._de_quality_dim(proposal.de_quality_multiplier),
            self._conviction_dim(proposal.de_conviction),
            self._advisor_dim(proposal.advisor_agreement),
            self._scan_score_dim(proposal.scan_score),
        ]

        product = 1.0
        for d in dims:
            product *= d.multiplier

        # ── Upstream gate-softening multipliers (Phase 9) ─────────────────
        # When an upstream QUALITY gate (scanner READY / OQ-EQ revalidation,
        # planner conviction floor, entry-score floor) softened a near-miss
        # instead of killing it, it handed through a bounded multiplier. Fold
        # each into the size so the marginal setup it rescued trades SMALL.
        # These are only added as dimensions when actually < 1.0 so a clean
        # full-quality setup's trace/sizing is identical to before.
        gate_floor = float(self._cfg("gate_quality_floor", 0.15))
        gate_mults = [
            ("scanner_gate", proposal.gate_quality_multiplier),
            ("planner_gate", proposal.planner_quality_multiplier),
            ("entry_gate", proposal.entry_quality_multiplier),
        ]
        softened = False
        for name, gm in gate_mults:
            try:
                gm = float(gm)
            except (TypeError, ValueError):
                continue
            if gm < 1.0 - 1e-9:
                gm = self._bound(gm, gate_floor, 1.0)
                product *= gm
                softened = True
                dims.append(DimensionContribution(
                    name, gm, gm, f"upstream gate softened — quality ×{gm:.2f}",
                ))

        size_floor = float(self._cfg("size_floor", 0.5))
        # A softened setup only reached the round table because an upstream gate
        # dimmed rather than killed it — let it size BELOW the analytic floor,
        # down to the gate floor, so a near-miss is a genuinely small trade.
        if softened:
            size_floor = min(size_floor, gate_floor)
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

    # ── Live position management (the round table for OPEN trades) ─────────
    # The entry path above answers "how big should I open this?".  An OPEN
    # position needs the same round table answering a continuous "how healthy is
    # this still?" every cycle — replacing the old argmax management collapse
    # (4 action scores → winner-takes-all, a 0.01 margin looking decisive) with a
    # graded health score that maps to a bounded management action.
    #
    # Crucial asymmetry vs entry: at entry a weak dimension only *dims* size,
    # because the trade was already approved — extinguishing it would refuse a
    # trade the pipeline wanted.  For an OPEN position the orchestrator is instead
    # *protecting capital already at risk*, so a destroyed dimension is allowed to
    # pull health all the way down to an EXIT.  The bound here is therefore
    # ``health_dimension_floor`` (default 0.0), distinct from the entry
    # ``dimension_floor``.  It remains a one-way dimmer: management can only HOLD,
    # tighten, scale down or exit — never oversize — and SCALE_UP is opt-in.

    def _health_dim_floor(self) -> float:
        return float(self._cfg("health_dimension_floor", 0.0))

    def _health_thresholds(self) -> dict:
        defaults = {
            "hold": 0.8,
            "tighten": 0.6,
            "scale_down": 0.4,
            "exit_partial": 0.2,
        }
        cfg = self._cfg("health_thresholds", None)
        if isinstance(cfg, dict):
            for k in defaults:
                if k in cfg:
                    try:
                        defaults[k] = float(cfg[k])
                    except (TypeError, ValueError):
                        pass
        return defaults

    def _hd(self, name: str, raw: float, mult: float, reason: str) -> DimensionContribution:
        floor = self._health_dim_floor()
        return DimensionContribution(name, self._bound(mult, floor, 1.0), raw, reason)

    def _thesis_integrity_dim(self, ev: "PositionEvidence") -> DimensionContribution:
        """Is the structural thesis that justified entry still intact?"""
        si = ev.structure_integrity
        if si is None:
            return self._hd("thesis_integrity", 0.0, 1.0, "no structure read (neutral)")
        # Structure integrity is already 0..1 (0 = broken, 1 = fully intact).
        return self._hd(
            "thesis_integrity", si, si,
            f"structure integrity {si:.2f}",
        )

    def _momentum_alignment_dim(self, ev: "PositionEvidence") -> DimensionContribution:
        """Is current momentum with (+) or against (−) the open position?"""
        m = ev.momentum
        if m is None:
            return self._hd("momentum_alignment", 0.0, 1.0, "no momentum read (neutral)")
        # Map signed momentum [-1, +1] → [0, 1]; against-trade momentum dims health.
        mult = (max(-1.0, min(1.0, m)) + 1.0) / 2.0
        return self._hd(
            "momentum_alignment", m, mult,
            f"momentum {m:+.2f} ({'with' if m >= 0 else 'against'} trade)",
        )

    def _risk_exposure_dim(self, ev: "PositionEvidence") -> DimensionContribution:
        """Account exposure right now — heat + how deep any drawdown runs."""
        heat = ev.portfolio_heat_pct or 0.0
        heat_full = float(self._cfg("risk_heat_full_pct", 100.0))
        heat_term = 1.0 - min(1.0, max(0.0, heat) / heat_full) if heat_full > 0 else 1.0
        # A position deep in loss is more exposed than one near flat.
        dd_term = 1.0
        if ev.profit_r is not None and ev.profit_r < 0:
            dd_full = float(self._cfg("risk_drawdown_full_r", 2.0))
            dd_term = 1.0 - min(1.0, abs(ev.profit_r) / dd_full) if dd_full > 0 else 1.0
        mult = heat_term * dd_term
        return self._hd(
            "risk_exposure", heat, mult,
            f"heat {heat:.1f}% drawdown {ev.profit_r if ev.profit_r is not None else 0.0:+.2f}R",
        )

    def _time_decay_dim(self, ev: "PositionEvidence") -> DimensionContribution:
        """A trade overstaying its expected horizon loses edge over time."""
        held = ev.hold_minutes or 0.0
        expected = ev.expected_hold_minutes or 0.0
        if expected <= 0 or held <= expected:
            return self._hd("time_decay", held, 1.0, f"held {held:.0f}m (within horizon)")
        # Linear decay from full at `expected` to floor at `decay_full`×expected.
        span_mult = float(self._cfg("time_decay_span_mult", 2.0))
        decay_end = expected * max(1.0, span_mult)
        if decay_end <= expected:
            return self._hd("time_decay", held, 1.0, f"held {held:.0f}m")
        frac = min(1.0, (held - expected) / (decay_end - expected))
        return self._hd(
            "time_decay", held, 1.0 - frac,
            f"held {held:.0f}m vs expected {expected:.0f}m (overstayed)",
        )

    def _profit_trajectory_dim(self, ev: "PositionEvidence") -> DimensionContribution:
        """Where the unrealised P&L sits — a loss dims, a winner stays healthy."""
        r = ev.profit_r
        if r is None:
            return self._hd("profit_trajectory", 0.0, 1.0, "no P&L read (neutral)")
        loss_full = float(self._cfg("profit_loss_full_r", 1.0))
        if r >= 0:
            return self._hd("profit_trajectory", r, 1.0, f"in profit {r:+.2f}R")
        frac = min(1.0, abs(r) / loss_full) if loss_full > 0 else 1.0
        return self._hd(
            "profit_trajectory", r, 1.0 - frac,
            f"in loss {r:+.2f}R vs full {loss_full:.1f}R",
        )

    def _situation_shift_dim(self, ev: "PositionEvidence") -> DimensionContribution:
        """How far the situation has moved against the trade since entry.

        With no entry baseline the dimension is neutral (1.0) — we can only
        measure *shift* when we know where we started.
        """
        if ev.entry_structure_integrity is None and ev.entry_tf_alignment is None:
            return self._hd("situation_shift", 0.0, 1.0, "no entry baseline (neutral)")
        adverse = 0.0
        parts = []
        if ev.entry_structure_integrity is not None and ev.structure_integrity is not None:
            drop = ev.entry_structure_integrity - ev.structure_integrity
            if drop > 0:
                adverse += drop
                parts.append(f"structure −{drop:.2f}")
        if ev.entry_tf_alignment is not None and ev.tf_alignment is not None:
            drop = ev.entry_tf_alignment - ev.tf_alignment
            if drop > 0:
                adverse += drop / 2.0  # tf_alignment spans [-1,1]; halve to 0..1 scale
                parts.append(f"HTF −{drop:.2f}")
        shift_full = float(self._cfg("situation_shift_full", 1.0))
        mult = 1.0 - min(1.0, adverse / shift_full) if shift_full > 0 else 1.0
        reason = ("situation steady" if adverse <= 0
                  else "situation shifted against (" + ", ".join(parts) + ")")
        return self._hd("situation_shift", adverse, mult, reason)

    def evaluate_open_position(self, evidence: "PositionEvidence") -> "PositionHealthReport":
        """Grade one OPEN position's current evidence into a health score + action.

        Pure function of the evidence + config (no broker / network / pandas), so
        it is trivially unit-testable and cannot stall the live loop.
        """
        allow_scale_up = bool(self._cfg("allow_scale_up", False))
        dims = [
            self._thesis_integrity_dim(evidence),
            self._momentum_alignment_dim(evidence),
            self._risk_exposure_dim(evidence),
            self._time_decay_dim(evidence),
            self._profit_trajectory_dim(evidence),
            self._situation_shift_dim(evidence),
        ]
        # Health is the GEOMETRIC MEAN of the dimension healths (not the raw
        # product used for entry sizing).  Entry deliberately compounds so weak
        # dimensions stack into a conservative dimmer, but a 6-way product would
        # make "HOLD" (>=0.8) almost unreachable for management and churn healthy
        # trades.  The geometric mean keeps the multiplicative character — one
        # fully-broken dimension (e.g. structure 0.0) still drives health to 0
        # and an exit — while reading naturally against the 0.8/0.6/0.4/0.2
        # thresholds as "average health across dimensions".
        product = 1.0
        for d in dims:
            product *= d.multiplier
        health = product ** (1.0 / len(dims)) if dims and product > 0 else 0.0
        health = self._bound(health, 0.0, 1.0)

        thresholds = self._health_thresholds()
        entry_health = evidence.entry_health
        health_delta = (health - entry_health) if entry_health is not None else 0.0

        action = self._action_for_health(health, thresholds, entry_health, allow_scale_up)

        recommended_size_pct: Optional[float] = None
        if action == ManagementAction.SCALE_DOWN:
            recommended_size_pct = float(self._cfg("scale_down_close_pct", 0.33))
        elif action == ManagementAction.EXIT_PARTIAL:
            recommended_size_pct = float(self._cfg("exit_partial_close_pct", 0.6))

        thesis_changes = [
            d.reason for d in dims
            if d.multiplier < float(self._cfg("thesis_change_flag", 0.6))
        ]

        report = PositionHealthReport(
            pair=evidence.pair,
            direction=evidence.direction,
            horizon=evidence.horizon,
            health_score=round(health, 4),
            action=action,
            dimension_scores={d.name: round(d.multiplier, 4) for d in dims},
            dimensions=dims,
            entry_health_at_open=entry_health,
            health_delta=round(health_delta, 4),
            thesis_changes=thesis_changes,
            recommended_sl=None,
            recommended_size_pct=recommended_size_pct,
        )
        logger.debug("[orchestrator/health] {}", report.summary())
        return report

    def _action_for_health(
        self, health: float, thresholds: dict,
        entry_health: Optional[float], allow_scale_up: bool,
    ) -> "ManagementAction":
        # SCALE_UP is opt-in and only when the trade is now demonstrably
        # healthier than at entry AND above an absolute bar — never just because
        # it is "fine".
        if allow_scale_up and entry_health is not None:
            scale_up_min = float(self._cfg("scale_up_min_health", 0.85))
            scale_up_delta = float(self._cfg("scale_up_min_delta", 0.1))
            if health >= scale_up_min and (health - entry_health) >= scale_up_delta:
                return ManagementAction.SCALE_UP
        if health >= thresholds["hold"]:
            return ManagementAction.HOLD
        if health >= thresholds["tighten"]:
            return ManagementAction.TIGHTEN_SL
        if health >= thresholds["scale_down"]:
            return ManagementAction.SCALE_DOWN
        if health >= thresholds["exit_partial"]:
            return ManagementAction.EXIT_PARTIAL
        return ManagementAction.EXIT_FULL

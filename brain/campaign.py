"""APEX TRADER — Campaign lifecycle (evolving market campaigns, not isolated trades).

The mandate's stance: Apex should stop thinking in isolated one-shot trades and
start thinking in **campaigns**. A campaign is the *lifetime of a directional
market thesis on a symbol*. It begins when sufficient evidence supports a
thesis, lives while that thesis keeps earning its standing with fresh evidence,
and ends only when the thesis is invalidated (evidence decays away or a contrary
thesis takes over). Over its lifetime a single campaign may open an initial
position, scale into favourable developments, take partials, re-enter after a
pullback, or reverse entirely — all *legs of one continuing idea* rather than a
scatter of unrelated orders.

Today the execution primitives for those legs already exist (scale-in, partials,
re-entry, reversal, displacement) but each spawns a *fresh, independent
position*: state is per-position, and nothing represents the campaign that spans
them. :class:`CampaignRegistry` is that missing aggregate. It sits alongside the
:class:`~brain.thesis_engine.ThesisEngine` (which answers "what is the market
saying *right now*") and answers the orthogonal question "what continuing idea
are we currently prosecuting, and how has it evolved".

Lifecycle (mirrors the thesis it tracks):

* **ACTIVE** — the supporting thesis is the dominant, actionable read; the
  campaign is being prosecuted and refreshed by fresh evidence.
* **DORMANT** — evidence stopped refreshing the thesis for
  ``dormant_after_seconds``; the idea is fading but not yet dead (a pullback or
  a quiet patch). A fresh actionable read revives it to ACTIVE.
* **INVALIDATED** — no fresh evidence for ``invalidate_after_seconds`` (the
  absence of confirming evidence is itself disconfirming) or a terminal
  contrary/stop close: the idea is over.
* **REVERSED** — the *opposing* thesis became the dominant actionable read; the
  campaign flipped direction (a new opposite-direction campaign is born).
* **COMPLETED** — the thesis was realised and the position closed on a
  target/secure/strategic exit.

Design principles (mirror the brain leaf modules, esp.
:mod:`brain.thesis_engine`):

* **Fail-safe.** Every public method swallows internal faults and returns a
  safe default — a campaign-tracking fault must never break the analysis or
  execution cycle.
* **Thread-safe.** A ``threading.Lock`` guards the store so the cycle path and a
  dashboard reader can touch it concurrently.
* **Bounded / pure-leaf.** Standard library + loguru only. It never imports a
  broker, the WorldModel, or any other brain module. It is handed plain numbers
  and strings (direction labels, exit-cause tokens) and keeps a bounded history.
* **Observational first.** Like the ThesisEngine (Gap 1a) before it, this layer
  is introduced *observationally* (default OFF, surfaced via Governance
  ``get_status``); it records the campaign narrative without altering any
  execution decision. Later sessions promote it into a driver.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Optional

from loguru import logger

# Directional labels — kept identical to brain.thesis_engine so the two layers
# speak the same vocabulary without importing each other.
LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"

# Leg kinds — the discrete order-level events that make up a campaign.
LEG_OPEN = "open"
LEG_SCALE_IN = "scale_in"
LEG_PARTIAL = "partial"
LEG_RE_ENTRY = "re_entry"
LEG_CLOSE = "close"

# Post-mortem verdicts — reasoning quality crossed with realised outcome. The
# mandate's stance made concrete: a win on thin evidence is a *process failure*
# (LUCKY), and a loss on strong, persistent evidence still *validates* the
# process (SOUND_BUT_UNLUCKY). Profit alone is an imperfect measure of decision
# quality, so campaigns are graded on both axes.
VERDICT_VALIDATED = "validated"                    # strong evidence + won
VERDICT_LUCKY = "lucky"                            # thin evidence + won (a process failure)
VERDICT_SOUND_BUT_UNLUCKY = "sound_but_unlucky"    # strong evidence + lost (process still sound)
VERDICT_DESERVED_LOSS = "deserved_loss"            # thin evidence + lost
VERDICT_INCONCLUSIVE = "inconclusive"              # too little evidence to judge

# Exit-cause tokens (mirror ``management.exit_cause.ExitCause`` *values*; kept as
# plain strings so this leaf module never imports the management package).
_PARTIAL_CLOSE_TOKENS = frozenset({"tp1_partial", "heat_trim"})
_REVERSAL_CLOSE_TOKENS = frozenset({"thesis_reversal"})
# Terminal closes where the thesis FAILED / was contradicted (idea invalidated).
_INVALIDATING_CLOSE_TOKENS = frozenset({
    "stop_loss", "evidence_exit", "thesis_flip", "thesis_invalidated",
    "thesis_silent", "invalidation", "conviction_collapse", "thesis_decay",
    "fast_opposition_decay", "structure_exit", "heat_emergency",
    "margin_flatten", "account_flatten",
})


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce to a finite float, falling back to ``default`` on NaN/inf/error."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf guard
        return default
    return f


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, _safe_float(value)))


def _norm_dir(direction: Any) -> str:
    d = str(direction or "").upper()
    return d if d in (LONG, SHORT) else FLAT


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _now_epoch() -> float:
    return time.time()


class CampaignState(Enum):
    """Where a campaign sits in its lifecycle (see module docstring)."""

    ACTIVE = "active"
    DORMANT = "dormant"
    INVALIDATED = "invalidated"
    REVERSED = "reversed"
    COMPLETED = "completed"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def is_terminal(self) -> bool:
        return self in (
            CampaignState.INVALIDATED,
            CampaignState.REVERSED,
            CampaignState.COMPLETED,
        )


@dataclass
class CampaignLeg:
    """One order-level event within a campaign (an open/scale/partial/close)."""

    kind: str
    at_iso: str
    at_epoch: float
    size: float = 0.0
    price: float = 0.0
    pnl: float = 0.0
    exit_cause: str = ""
    ticket: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "at": self.at_iso,
            "size": round(self.size, 6),
            "price": round(self.price, 6),
            "pnl": round(self.pnl, 4),
            "exit_cause": self.exit_cause,
            "ticket": self.ticket,
        }


@dataclass
class CampaignPostmortem:
    """Autonomous grade of a terminated campaign on reasoning quality vs outcome.

    ``reasoning_quality`` (== ``evidence_support``) is a 0..1 read of how well
    the thesis *earned its standing* over the campaign's life — persistence
    (how many times fresh evidence renewed it) blended with strength (average
    supporting confidence). It is deliberately decoupled from P&L.

    ``verdict`` crosses that with the realised outcome; ``confidence_delta`` is
    the suggested nudge to the process's standing that the learning layer can
    later consume (positive = trust the reasoning more, negative = less). Note
    the asymmetry the mandate demands: LUCKY (won on thin evidence) yields a
    *negative* delta — luck must not be rewarded — while SOUND_BUT_UNLUCKY
    (lost on strong evidence) yields a small *positive* delta.
    """

    verdict: str
    reasoning_quality: float            # 0..1 — evidence support (persistence × strength)
    evidence_support: float             # 0..1 — alias kept explicit for the trace
    outcome_won: bool
    realized_pnl: float
    confidence_delta: float             # suggested authority nudge for the process
    refresh_count: int
    peak_ev_over_flat: float
    duration_seconds: float
    leg_count: int
    narrative: str = ""

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reasoning_quality": round(self.reasoning_quality, 4),
            "evidence_support": round(self.evidence_support, 4),
            "outcome_won": bool(self.outcome_won),
            "realized_pnl": round(self.realized_pnl, 4),
            "confidence_delta": round(self.confidence_delta, 4),
            "refresh_count": int(self.refresh_count),
            "peak_ev_over_flat": round(self.peak_ev_over_flat, 4),
            "duration_seconds": round(self.duration_seconds, 1),
            "leg_count": int(self.leg_count),
            "narrative": self.narrative,
        }


@dataclass
class Campaign:
    """The lifetime of one directional market thesis on a symbol."""

    campaign_id: str
    symbol: str
    direction: str                       # LONG | SHORT
    state: CampaignState = CampaignState.ACTIVE
    ev_over_flat: float = 0.0            # latest thesis edge over the do-nothing baseline (R)
    confidence: float = 0.0             # latest supporting-evidence confidence (0..1)
    peak_ev_over_flat: float = 0.0      # best edge the thesis ever reached
    confidence_sum: float = 0.0         # running sum of refresh confidences (for the mean)
    opened_at_iso: str = ""
    opened_at_epoch: float = 0.0
    last_refreshed_iso: str = ""
    last_refreshed_epoch: float = 0.0
    ended_at_iso: str = ""
    ended_reason: str = ""
    refresh_count: int = 0              # times fresh actionable evidence renewed the thesis
    realized_pnl: float = 0.0           # summed realised P&L across closed legs
    legs: list[CampaignLeg] = field(default_factory=list)
    postmortem: Optional[CampaignPostmortem] = None

    # ── Derived reads ────────────────────────────────────────────────────
    @property
    def open_legs(self) -> int:
        """Net exposure-adding legs (open/scale/re-entry) minus full closes."""
        adds = sum(1 for lg in self.legs
                   if lg.kind in (LEG_OPEN, LEG_SCALE_IN, LEG_RE_ENTRY))
        return adds

    def staleness_seconds(self, now: Optional[float] = None) -> float:
        t = _now_epoch() if now is None else _safe_float(now)
        last = self.last_refreshed_epoch or self.opened_at_epoch
        return max(0.0, t - last) if last > 0 else 0.0

    def refresh(self, ev_over_flat: float, confidence: float, now_iso: str,
                now_epoch: float) -> None:
        """Fresh actionable evidence renewed this thesis — revive + record."""
        self.ev_over_flat = _safe_float(ev_over_flat)
        self.confidence = _clamp01(confidence)
        self.peak_ev_over_flat = max(self.peak_ev_over_flat, self.ev_over_flat)
        self.confidence_sum += self.confidence
        self.last_refreshed_iso = now_iso
        self.last_refreshed_epoch = now_epoch
        self.refresh_count += 1
        if self.state == CampaignState.DORMANT:
            self.state = CampaignState.ACTIVE

    @property
    def avg_confidence(self) -> float:
        """Mean supporting confidence across all evidence refreshes."""
        return self.confidence_sum / self.refresh_count if self.refresh_count else 0.0

    def to_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "state": self.state.value,
            "ev_over_flat": round(self.ev_over_flat, 4),
            "confidence": round(self.confidence, 4),
            "peak_ev_over_flat": round(self.peak_ev_over_flat, 4),
            "avg_confidence": round(self.avg_confidence, 4),
            "opened_at": self.opened_at_iso,
            "last_refreshed": self.last_refreshed_iso,
            "ended_at": self.ended_at_iso,
            "ended_reason": self.ended_reason,
            "refresh_count": int(self.refresh_count),
            "open_legs": int(self.open_legs),
            "leg_count": len(self.legs),
            "realized_pnl": round(self.realized_pnl, 4),
            "age_seconds": round(self.staleness_seconds(), 1),
            "postmortem": self.postmortem.to_dict() if self.postmortem is not None else None,
        }


class CampaignRegistry:
    """Tracks the evolving campaign per (symbol, direction). Fail-safe.

    One *live* campaign is kept per ``(symbol, direction)`` key; when it ends it
    is moved to a bounded ``finalized`` history so the post-mortem / dashboard
    can still read it. All public methods are best-effort and never raise into
    the caller.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        dormant_after_seconds: float = 900.0,
        invalidate_after_seconds: float = 3600.0,
        history_limit: int = 500,
        postmortem_enabled: bool = True,
        sound_evidence_threshold: float = 0.5,
        evidence_full_refreshes: int = 5,
    ) -> None:
        self.enabled = bool(enabled)
        da = _safe_float(dormant_after_seconds, 900.0)
        ia = _safe_float(invalidate_after_seconds, 3600.0)
        self.dormant_after_seconds = da if da > 0 else 900.0
        # Invalidation must not precede dormancy — clamp up if misconfigured.
        self.invalidate_after_seconds = max(ia, self.dormant_after_seconds)
        self._history_limit = max(1, int(history_limit))
        self.postmortem_enabled = bool(postmortem_enabled)
        self.sound_evidence_threshold = min(1.0, max(0.0,
            _safe_float(sound_evidence_threshold, 0.5)))
        self.evidence_full_refreshes = max(1, int(evidence_full_refreshes))
        self._live: dict[tuple[str, str], Campaign] = {}
        self._finalized: Deque[Campaign] = deque(maxlen=self._history_limit)
        self._seq = 0
        self._lock = threading.Lock()

    # ── Ingest: evidence (thesis) ─────────────────────────────────────────

    def observe_thesis(
        self,
        symbol: str,
        direction: str,
        should_act: bool,
        ev_over_flat: float = 0.0,
        confidence: float = 0.0,
        now: Optional[float] = None,
    ) -> Optional[Campaign]:
        """Fold one dominant-thesis read into the campaign store.

        When ``should_act`` is truthy and ``direction`` is directional, the
        matching campaign is born (or revived + refreshed). Any *opposing* live
        campaign on the same symbol is flipped to REVERSED — the market
        narrative changed. When ``should_act`` is falsy this is a no-op; the
        time-based :meth:`decay` handles the fading of an unrefreshed idea.
        Returns the live campaign for the direction (or ``None``). Fail-safe.
        """
        try:
            sym = str(symbol or "")
            d = _norm_dir(direction)
            if not should_act or d == FLAT or not sym:
                return None
            t = _now_epoch() if now is None else _safe_float(now)
            now_iso = _now_iso()
            with self._lock:
                # A fresh actionable read the OTHER way flips the standing idea.
                opp = SHORT if d == LONG else LONG
                opp_camp = self._live.get((sym, opp))
                if opp_camp is not None:
                    self._finalize_locked(
                        opp_camp, CampaignState.REVERSED, t, "opposing_thesis_dominant"
                    )

                camp = self._live.get((sym, d))
                if camp is None:
                    self._seq += 1
                    camp = Campaign(
                        campaign_id=f"{sym}:{d}:{self._seq}",
                        symbol=sym,
                        direction=d,
                        state=CampaignState.ACTIVE,
                        opened_at_iso=now_iso,
                        opened_at_epoch=t,
                    )
                    self._live[(sym, d)] = camp
                    logger.debug(
                        "[campaign] OPEN {} — ev-over-flat {:.3f}R conf {:.2f}",
                        camp.campaign_id, _safe_float(ev_over_flat),
                        _clamp01(confidence),
                    )
                camp.refresh(ev_over_flat, confidence, now_iso, t)
                return camp
        except Exception as exc:  # noqa: BLE001 — tracking must never break a cycle
            logger.debug("[campaign] observe_thesis({}) ignored a fault: {}",
                         symbol, exc)
            return None

    # ── Ingest: order legs ────────────────────────────────────────────────

    def record_leg(
        self,
        symbol: str,
        direction: str,
        kind: str,
        *,
        size: float = 0.0,
        price: float = 0.0,
        pnl: float = 0.0,
        ticket: str = "",
        now: Optional[float] = None,
    ) -> None:
        """Attach an exposure-adding leg (open/scale/re-entry) to the campaign.

        No-op if no live campaign exists for the key (an order without a tracked
        supporting thesis is not forced into a synthetic campaign). Fail-safe.
        """
        try:
            sym = str(symbol or "")
            d = _norm_dir(direction)
            with self._lock:
                camp = self._live.get((sym, d))
                if camp is None:
                    return
                t = _now_epoch() if now is None else _safe_float(now)
                camp.legs.append(CampaignLeg(
                    kind=str(kind or LEG_OPEN),
                    at_iso=_now_iso(),
                    at_epoch=t,
                    size=_safe_float(size),
                    price=_safe_float(price),
                    pnl=_safe_float(pnl),
                    ticket=str(ticket or ""),
                ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[campaign] record_leg({}) ignored a fault: {}",
                         symbol, exc)

    def observe_close(
        self,
        symbol: str,
        direction: str,
        exit_cause: str = "",
        *,
        pnl: float = 0.0,
        won: Optional[bool] = None,
        ticket: str = "",
        now: Optional[float] = None,
    ) -> Optional[Campaign]:
        """Record a closed leg and, when terminal, end the campaign.

        The ``exit_cause`` token (a :class:`~management.exit_cause.ExitCause`
        value string) decides whether this close is a *partial* (campaign
        continues), a *reversal* (campaign flips → REVERSED), or a *terminal*
        exit (COMPLETED when the thesis was realised, INVALIDATED when it
        failed). Fail-safe.
        """
        try:
            sym = str(symbol or "")
            d = _norm_dir(direction)
            cause = str(exit_cause or "").strip().lower()
            t = _now_epoch() if now is None else _safe_float(now)
            with self._lock:
                camp = self._live.get((sym, d))
                if camp is None:
                    return None
                camp.legs.append(CampaignLeg(
                    kind=LEG_CLOSE,
                    at_iso=_now_iso(),
                    at_epoch=t,
                    pnl=_safe_float(pnl),
                    exit_cause=cause,
                    ticket=str(ticket or ""),
                ))
                camp.realized_pnl += _safe_float(pnl)

                if cause in _PARTIAL_CLOSE_TOKENS:
                    # A partial secures profit but the campaign lives on.
                    return camp
                if cause in _REVERSAL_CLOSE_TOKENS:
                    self._finalize_locked(camp, CampaignState.REVERSED, t, cause)
                    return camp
                # Any other close ends the campaign; classify realised vs failed.
                if cause in _INVALIDATING_CLOSE_TOKENS or won is False:
                    end_state = CampaignState.INVALIDATED
                else:
                    end_state = CampaignState.COMPLETED
                self._finalize_locked(camp, end_state, t, cause or "close")
                return camp
        except Exception as exc:  # noqa: BLE001 — a fault must never break a close
            logger.debug("[campaign] observe_close({}) ignored a fault: {}",
                         symbol, exc)
            return None

    # ── Decay (time-based lifecycle transitions) ──────────────────────────

    def decay(self, now: Optional[float] = None) -> None:
        """Age live campaigns: ACTIVE→DORMANT→INVALIDATED as evidence goes stale.

        The absence of confirming evidence is itself disconfirming (mirrors the
        ThesisEngine's time decay): a campaign nothing has refreshed for
        ``dormant_after_seconds`` fades to DORMANT, and past
        ``invalidate_after_seconds`` the idea is declared dead. Fail-safe.
        """
        try:
            t = _now_epoch() if now is None else _safe_float(now)
            with self._lock:
                for camp in list(self._live.values()):
                    stale = camp.staleness_seconds(t)
                    if stale >= self.invalidate_after_seconds:
                        self._finalize_locked(
                            camp, CampaignState.INVALIDATED, t, "evidence_stale"
                        )
                    elif (camp.state == CampaignState.ACTIVE
                          and stale >= self.dormant_after_seconds):
                        camp.state = CampaignState.DORMANT
                        logger.debug(
                            "[campaign] DORMANT {} — {:.0f}s without fresh evidence",
                            camp.campaign_id, stale,
                        )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[campaign] decay ignored a fault: {}", exc)

    # ── Finalisation ──────────────────────────────────────────────────────

    def _finalize_locked(
        self, camp: Campaign, state: CampaignState, now: float, reason: str,
    ) -> None:
        """Move a live campaign into terminal history. Caller holds the lock."""
        camp.state = state
        camp.ended_at_iso = _now_iso()
        camp.ended_reason = str(reason or "")
        if self.postmortem_enabled:
            camp.postmortem = self._run_postmortem(camp, now)
        self._live.pop((camp.symbol, camp.direction), None)
        self._finalized.append(camp)
        logger.debug(
            "[campaign] {} {} — {} legs, realised {:.2f}, reason={}{}",
            state.value.upper(), camp.campaign_id,
            len(camp.legs), camp.realized_pnl, reason,
            f", verdict={camp.postmortem.verdict}" if camp.postmortem else "",
        )

    def _run_postmortem(self, camp: Campaign, now: float) -> CampaignPostmortem:
        """Grade a terminated campaign on reasoning quality vs realised outcome.

        Reasoning quality is evidence *support*: persistence (refresh count,
        saturating at ``evidence_full_refreshes``) blended 50/50 with strength
        (mean supporting confidence). Crossed with the win/loss outcome it
        yields one of the :data:`VERDICT_*` labels and a suggested
        ``confidence_delta`` — positive to trust the process more, negative to
        trust it less. Luck (a win on thin evidence) is penalised; a sound but
        unlucky loss is mildly rewarded. Pure — never raises (caller wraps it).
        """
        persistence = min(1.0, camp.refresh_count / float(self.evidence_full_refreshes))
        strength = _clamp01(camp.avg_confidence)
        support = _clamp01(0.5 * persistence + 0.5 * strength)
        won = camp.realized_pnl > 0.0
        thr = self.sound_evidence_threshold

        if camp.refresh_count <= 0:
            verdict = VERDICT_INCONCLUSIVE
            delta = 0.0
            narrative = "no evidence refreshes recorded — too little to judge"
        elif won and support >= thr:
            verdict = VERDICT_VALIDATED
            delta = round(0.5 * support, 4)
            narrative = (
                f"won on strong, persistent evidence (support {support:.2f}) — "
                "the reasoning is confirmed"
            )
        elif won and support < thr:
            verdict = VERDICT_LUCKY
            delta = round(-0.25 * (thr - support), 4)
            narrative = (
                f"won despite thin evidence (support {support:.2f} < {thr:.2f}) — "
                "a favourable outcome from weak reasoning; do not reward the luck"
            )
        elif (not won) and support >= thr:
            verdict = VERDICT_SOUND_BUT_UNLUCKY
            delta = round(0.15 * support, 4)
            narrative = (
                f"lost despite strong evidence (support {support:.2f}) — the "
                "process was sound; a single adverse outcome does not invalidate it"
            )
        else:
            verdict = VERDICT_DESERVED_LOSS
            delta = round(-0.4 * (thr - support + 0.1), 4)
            narrative = (
                f"lost on thin evidence (support {support:.2f} < {thr:.2f}) — the "
                "reasoning was weak and the outcome matched"
            )

        duration = max(0.0, _safe_float(now) - camp.opened_at_epoch) \
            if camp.opened_at_epoch else 0.0
        return CampaignPostmortem(
            verdict=verdict,
            reasoning_quality=support,
            evidence_support=support,
            outcome_won=won,
            realized_pnl=_safe_float(camp.realized_pnl),
            confidence_delta=delta,
            refresh_count=int(camp.refresh_count),
            peak_ev_over_flat=_safe_float(camp.peak_ev_over_flat),
            duration_seconds=duration,
            leg_count=len(camp.legs),
            narrative=narrative,
        )

    # ── Introspection ─────────────────────────────────────────────────────

    def get(self, symbol: str, direction: str) -> Optional[Campaign]:
        """Return the live campaign for a key (or ``None``). Fail-safe."""
        try:
            with self._lock:
                return self._live.get((str(symbol or ""), _norm_dir(direction)))
        except Exception:  # noqa: BLE001
            return None

    def get_status(self) -> dict:
        """Summary of live + recent campaigns for the dashboard / governance."""
        try:
            with self._lock:
                live = list(self._live.values())
                finalized = list(self._finalized)
            active = sum(1 for c in live if c.state == CampaignState.ACTIVE)
            dormant = sum(1 for c in live if c.state == CampaignState.DORMANT)
            term_counts: dict[str, int] = {}
            verdict_counts: dict[str, int] = {}
            for c in finalized:
                term_counts[c.state.value] = term_counts.get(c.state.value, 0) + 1
                if c.postmortem is not None:
                    v = c.postmortem.verdict
                    verdict_counts[v] = verdict_counts.get(v, 0) + 1
            # Cap the per-campaign detail so a large book can't bloat status.
            live_out = [c.to_dict() for c in live[:50]]
            recent_out = [c.to_dict() for c in list(finalized)[-25:]]
            return {
                "enabled": self.enabled,
                "live_campaigns": len(live),
                "active": active,
                "dormant": dormant,
                "finalized_tracked": len(finalized),
                "terminal_counts": term_counts,
                "postmortem_verdicts": verdict_counts,
                "dormant_after_seconds": self.dormant_after_seconds,
                "invalidate_after_seconds": self.invalidate_after_seconds,
                "live": live_out,
                "recent": recent_out,
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[campaign] get_status ignored a fault: {}", exc)
            return {"enabled": self.enabled, "live_campaigns": 0, "live": []}

    def reset(self) -> None:
        """Drop all tracked campaigns (tests / manual ops reset)."""
        with self._lock:
            self._live.clear()
            self._finalized.clear()
            self._seq = 0


__all__ = [
    "Campaign",
    "CampaignLeg",
    "CampaignPostmortem",
    "CampaignState",
    "CampaignRegistry",
    "LONG",
    "SHORT",
    "FLAT",
    "LEG_OPEN",
    "LEG_SCALE_IN",
    "LEG_PARTIAL",
    "LEG_RE_ENTRY",
    "LEG_CLOSE",
    "VERDICT_VALIDATED",
    "VERDICT_LUCKY",
    "VERDICT_SOUND_BUT_UNLUCKY",
    "VERDICT_DESERVED_LOSS",
    "VERDICT_INCONCLUSIVE",
]

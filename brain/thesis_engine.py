"""APEX TRADER — Thesis Engine (Gap 1a: persistent competing theses).

The Consensus Division (:mod:`brain.directional_consensus`) collapses the vote
panel to a single winning direction every cycle — the losing side's evidence is
discarded the instant the net is summed. The APEX vision requires the opposite:
keep a **Long thesis**, a **Short thesis**, and a **Flat thesis** alive
simultaneously, each carrying its own expected value, confidence, and
uncertainty, and choose the *dominant* one on relative expected value rather
than on which side won a vote sum.

:class:`ThesisEngine` is that store. On every analysis cycle it is fed the vote
panel and the probabilistic bias for a symbol and rebuilds all three theses:

* **Long** — built from the LONG votes (confidence, support magnitude, modules)
  with EV = ``entry_ev_long`` and ``probability = long_probability``.
* **Short** — the mirror, from the SHORT votes.
* **Flat** — the "do nothing" thesis. EV = ``flat_ev`` (0 by default), its
  confidence rising as the directional theses weaken. Flat is always available
  and **never decays** — doing nothing is the baseline the directional theses
  must beat.

``uncertainty`` per directional thesis is the opposing/total signed-magnitude
ratio: a long thesis with heavy short opposition is uncertain even if it
nominally "wins". ``dominant`` is simply the highest-EV thesis (Flat included),
and :meth:`should_act` only signals a trade when the dominant directional thesis
exceeds Flat by ``min_ev_threshold`` — so a marginal edge over doing nothing is
not enough.

:meth:`decay_all` ages every stored thesis on bar-close: an idea that stops
being refreshed by new evidence has its confidence multiplied down toward zero
(``decay_rate`` per bar). Flat is exempt. This is "no opinion is permanent" made
concrete — a stale directional thesis loses standing automatically.

Session 27 adds **time-based decay** on top of that per-bar aging. Where
``decay_all`` ages *confidence* discretely on each bar-close, the time decay
continuously erodes a thesis's *effective EV* the longer it goes without a fresh
evidence refresh, computed **on read** (never stored, no timer)::

    effective_ev = base_ev * exp(-ln2 / half_life * seconds_since_refresh)

The absence of confirming evidence is itself disconfirming: a Long thesis that
nothing has refreshed for ``decay_half_life`` seconds is worth half its EV, so
it silently loses its edge over the Flat baseline and stops clearing
:meth:`should_act`. Decay applies to Long, Short **and** Flat (with the default
``flat_ev`` = 0 the Flat decay is a no-op, but the comparison stays fair). A
fresh :meth:`update` resets the decay clock. :meth:`challenge` /
:meth:`challenge_all` surface the meaningful transitions — a thesis decaying
below the action threshold, expiring below the floor, or a decay-induced change
of the dominant side — as edge-triggered log lines.

This session wires the engine **observationally** (it runs in parallel with the
existing consensus pipeline and is surfaced via Governance ``get_status``); it
does not yet drive entries. Later sessions consume its output.

Design principles (mirror the Governance department leaf modules):

* **Fail-safe.** Every public method swallows internal faults and returns a safe
  default — a thesis-tracking fault must never break the analysis cycle.
* **Thread-safe.** A ``threading.Lock`` guards the per-symbol store so the
  cycle path and a dashboard reader can touch it concurrently.
* **Bounded / pure-leaf.** Standard library + loguru only. It never imports a
  broker, the WorldModel, or any brain module — it is handed plain numbers and
  vote-like objects (anything exposing ``module`` / ``direction`` /
  ``confidence`` / ``weight``).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from loguru import logger

# Directional labels. FLAT is the explicit "do nothing" thesis.
LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce to a finite float, falling back to ``default`` on NaN/inf/error."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf guard
        return default
    return f


@dataclass
class Thesis:
    """One side of the market (LONG / SHORT / FLAT) as a standing hypothesis."""

    symbol: str
    direction: str                          # LONG | SHORT | FLAT
    ev: float = 0.0                         # expected value in R-multiples (may be < 0)
    confidence: float = 0.0                 # 0..1 — strength of the supporting evidence
    uncertainty: float = 0.0                # 0..1 — opposing/total signed-magnitude ratio
    supporting_modules: list[str] = field(default_factory=list)
    opposing_modules: list[str] = field(default_factory=list)
    vote_weight: float = 0.0                # total signed-magnitude backing this side
    probability: float = 0.0               # directional probability from the bias model
    last_updated: str = ""                  # ISO timestamp of last refresh
    last_refreshed_at: float = 0.0          # epoch seconds of last refresh (time-decay clock)
    age_bars: int = 0                       # bar-closes since last refresh
    decay_factor: float = 1.0               # 1.0 = fresh; decays toward 0 when stale

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "ev": round(self.ev, 4),
            "confidence": round(self.confidence, 4),
            "uncertainty": round(self.uncertainty, 4),
            "supporting_modules": list(self.supporting_modules),
            "opposing_modules": list(self.opposing_modules),
            "vote_weight": round(self.vote_weight, 4),
            "probability": round(self.probability, 4),
            "last_updated": self.last_updated,
            "last_refreshed_at": round(self.last_refreshed_at, 3),
            "age_bars": int(self.age_bars),
            "decay_factor": round(self.decay_factor, 4),
        }


@dataclass
class ThesisSet:
    """The three competing theses for one symbol plus the dominance read."""

    symbol: str
    long_thesis: Thesis
    short_thesis: Thesis
    flat_thesis: Thesis
    dominant: str = FLAT                    # direction of the highest-EV thesis (at build time)
    dominant_ev_advantage: float = 0.0      # best EV minus second-best EV
    updated_at: str = ""
    # Edge-triggered flags for the continuous-challenge logging (Session 27).
    # They ride on the ThesisSet so a fresh update() (which rebuilds the set)
    # naturally resets them — each evidence generation logs its decay
    # transitions at most once. Excluded from ``to_dict`` (internal only).
    decay_below_threshold_logged: bool = field(default=False, repr=False)
    decay_expired_logged: bool = field(default=False, repr=False)
    decay_flip_logged: bool = field(default=False, repr=False)

    def theses(self) -> tuple[Thesis, Thesis, Thesis]:
        return (self.long_thesis, self.short_thesis, self.flat_thesis)

    def get(self, direction: str) -> Thesis:
        d = str(direction or "").upper()
        if d == LONG:
            return self.long_thesis
        if d == SHORT:
            return self.short_thesis
        return self.flat_thesis

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "long": self.long_thesis.to_dict(),
            "short": self.short_thesis.to_dict(),
            "flat": self.flat_thesis.to_dict(),
            "dominant": self.dominant,
            "dominant_ev_advantage": round(self.dominant_ev_advantage, 4),
            "updated_at": self.updated_at,
        }


class ThesisEngine:
    """Maintains persistent competing Long/Short/Flat theses per symbol."""

    def __init__(
        self,
        *,
        min_ev_threshold: float = 0.3,
        decay_rate: float = 0.95,
        flat_ev: float = 0.0,
        decay_enabled: bool = True,
        decay_half_life: float = 900.0,
        decay_floor: float = 0.01,
    ) -> None:
        self.min_ev_threshold = float(min_ev_threshold)
        # Clamp decay into (0, 1] — a rate >= 1 would never decay, <= 0 would
        # erase an idea on the first bar.
        self.decay_rate = min(1.0, max(0.0, float(decay_rate)))
        self.flat_ev = float(flat_ev)
        # ── Session 27: time-based (compute-on-read) EV decay ──────────────
        self.decay_enabled = bool(decay_enabled)
        hl = float(decay_half_life)
        # Half-life must be positive — fall back to the 15-minute default rather
        # than divide by zero when handed a bad value.
        self.decay_half_life = hl if hl > 0.0 else 900.0
        self.decay_floor = max(0.0, float(decay_floor))
        # exp(-lambda * dt) with lambda = ln2/half_life gives 0.5 at one half-life.
        self._decay_lambda = math.log(2.0) / self.decay_half_life
        self._sets: dict[str, ThesisSet] = {}
        self._lock = threading.Lock()

    # ── Ingest ──────────────────────────────────────────────────────────

    def update(
        self,
        symbol: str,
        votes: Iterable[Any],
        long_probability: float,
        short_probability: float,
        entry_ev_long: float,
        entry_ev_short: float,
    ) -> Optional[ThesisSet]:
        """Rebuild the three competing theses for ``symbol`` from fresh evidence.

        ``votes`` is any iterable of vote-like objects exposing ``module``,
        ``direction``, ``confidence`` and ``weight`` (e.g.
        :class:`brain.directional_consensus.Vote`). Fail-safe: returns ``None``
        on any internal error rather than raising into the analysis cycle.
        """
        try:
            sym = str(symbol or "")
            vote_list = list(votes or [])
            now_iso = _now_iso()
            now_epoch = _now_epoch()

            long_t = self._build_directional(
                sym, LONG, vote_list,
                ev=_safe_float(entry_ev_long),
                probability=_safe_float(long_probability),
                now_iso=now_iso,
                now_epoch=now_epoch,
            )
            short_t = self._build_directional(
                sym, SHORT, vote_list,
                ev=_safe_float(entry_ev_short),
                probability=_safe_float(short_probability),
                now_iso=now_iso,
                now_epoch=now_epoch,
            )
            # Flat confidence rises as the directional theses weaken — when
            # neither side has strong support, doing nothing is the strong read.
            flat_conf = max(0.0, 1.0 - max(long_t.confidence, short_t.confidence))
            flat_t = Thesis(
                symbol=sym,
                direction=FLAT,
                ev=self.flat_ev,
                confidence=flat_conf,
                uncertainty=0.0,
                vote_weight=0.0,
                probability=0.0,
                last_updated=now_iso,
                last_refreshed_at=now_epoch,
                age_bars=0,
                decay_factor=1.0,
            )

            tset = self._assemble(sym, long_t, short_t, flat_t, now_iso)
            with self._lock:
                self._sets[sym] = tset
            return tset
        except Exception as exc:  # noqa: BLE001 — tracking must never break the cycle
            logger.debug("[thesis] update({}) ignored a fault: {}", symbol, exc)
            return None

    def _build_directional(
        self,
        symbol: str,
        direction: str,
        votes: list[Any],
        *,
        ev: float,
        probability: float,
        now_iso: str,
        now_epoch: float,
    ) -> Thesis:
        same: list[Any] = []
        opp: list[Any] = []
        opp_dir = SHORT if direction == LONG else LONG
        for v in votes:
            vdir = str(getattr(v, "direction", "") or "").upper()
            if vdir == direction:
                same.append(v)
            elif vdir == opp_dir:
                opp.append(v)

        support_mag = sum(_signed_magnitude(v) for v in same)
        oppose_mag = sum(_signed_magnitude(v) for v in opp)
        total_mag = support_mag + oppose_mag

        if support_mag > 0.0:
            # Weight-weighted mean confidence of the supporting votes.
            w = sum(max(0.0, _safe_float(getattr(v, "weight", 0.0))) for v in same)
            if w > 0.0:
                confidence = sum(
                    max(0.0, _safe_float(getattr(v, "weight", 0.0)))
                    * _clamp01(_safe_float(getattr(v, "confidence", 0.0)))
                    for v in same
                ) / w
            else:
                confidence = 0.0
        else:
            confidence = 0.0

        uncertainty = (oppose_mag / total_mag) if total_mag > 0.0 else 1.0

        return Thesis(
            symbol=symbol,
            direction=direction,
            ev=ev,
            confidence=_clamp01(confidence),
            uncertainty=_clamp01(uncertainty),
            supporting_modules=[str(getattr(v, "module", "")) for v in same],
            opposing_modules=[str(getattr(v, "module", "")) for v in opp],
            vote_weight=support_mag,
            probability=_clamp01(probability),
            last_updated=now_iso,
            last_refreshed_at=now_epoch,
            age_bars=0,
            decay_factor=1.0,
        )

    def _assemble(
        self,
        symbol: str,
        long_t: Thesis,
        short_t: Thesis,
        flat_t: Thesis,
        now_iso: str,
    ) -> ThesisSet:
        ranked = sorted(
            (long_t, short_t, flat_t), key=lambda t: t.ev, reverse=True,
        )
        dominant = ranked[0]
        advantage = ranked[0].ev - ranked[1].ev
        return ThesisSet(
            symbol=symbol,
            long_thesis=long_t,
            short_thesis=short_t,
            flat_thesis=flat_t,
            dominant=dominant.direction,
            dominant_ev_advantage=advantage,
            updated_at=now_iso,
        )

    # ── Time decay (compute-on-read) ─────────────────────────────────────

    def _time_decay_factor(self, thesis: Thesis, now: float) -> float:
        """Multiplier in ``(0, 1]`` for how much of ``thesis`` EV still counts.

        ``exp(-ln2/half_life * seconds_since_refresh)`` — 1.0 when just
        refreshed, 0.5 after one half-life, trending to 0 as the idea goes
        stale. Fail-safe: returns 1.0 (un-decayed) on any error or when decay is
        disabled, so a decay fault can never erase a live thesis.
        """
        if not self.decay_enabled:
            return 1.0
        try:
            last = _safe_float(getattr(thesis, "last_refreshed_at", 0.0))
            if last <= 0.0:
                return 1.0
            dt = now - last
            if dt <= 0.0:
                return 1.0
            factor = math.exp(-self._decay_lambda * dt)
            if factor != factor:  # NaN guard
                return 1.0
            return min(1.0, max(0.0, factor))
        except Exception as exc:  # noqa: BLE001 — never erase a thesis on a fault
            logger.warning(
                "[thesis] decay factor failed for {} — using base EV: {}",
                getattr(thesis, "symbol", "?"), exc,
            )
            return 1.0

    def _effective_ev(self, thesis: Thesis, now: float) -> float:
        """Base EV scaled by the time-decay factor (the value decisions use)."""
        return _safe_float(thesis.ev) * self._time_decay_factor(thesis, now)

    def _effective_confidence(self, thesis: Thesis, now: float) -> float:
        """Confidence scaled by the time-decay factor (for the dashboard)."""
        return _clamp01(
            _safe_float(thesis.confidence) * self._time_decay_factor(thesis, now)
        )

    def _decayed_view(
        self, tset: ThesisSet, now: float,
    ) -> tuple[dict[str, float], str, float]:
        """Effective (decayed) EVs and the dominant side at ``now``.

        Returns ``({LONG: ev, SHORT: ev, FLAT: ev}, dominant_direction,
        dominant_ev)``. Dominance is recomputed from the decayed EVs — a stale
        idea can lose the lead to a freshly-refreshed opposing one. Tie-break
        order (LONG, SHORT, FLAT) mirrors :meth:`_assemble` so the decay-time
        dominant matches the build-time dominant when nothing has decayed.
        """
        evs = {
            LONG: self._effective_ev(tset.long_thesis, now),
            SHORT: self._effective_ev(tset.short_thesis, now),
            FLAT: self._effective_ev(tset.flat_thesis, now),
        }
        dominant = max((LONG, SHORT, FLAT), key=lambda d: evs[d])
        return evs, dominant, evs[dominant]

    # ── Read ────────────────────────────────────────────────────────────

    def get(self, symbol: str) -> Optional[ThesisSet]:
        """Return the current thesis set for ``symbol`` (or ``None``)."""
        try:
            with self._lock:
                return self._sets.get(str(symbol or ""))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[thesis] get({}) ignored a fault: {}", symbol, exc)
            return None

    def should_act(
        self, symbol: str, now: Optional[float] = None,
    ) -> tuple[bool, str, float]:
        """Should APEX act on ``symbol`` right now?

        Returns ``(should_trade, direction, ev_advantage)``. ``should_trade`` is
        True only when the dominant thesis is directional (not FLAT) **and** its
        *effective* (time-decayed) EV exceeds Flat's effective EV by at least
        ``min_ev_threshold`` — a marginal edge over doing nothing is deliberately
        not enough, and a thesis that has gone stale (decayed below the margin)
        stops being actionable even though its base EV is unchanged. Dominance is
        recomputed from the decayed EVs, so a stale winner can lose the lead to a
        freshly-refreshed opposing thesis. Reading also runs the continuous
        challenge (edge-triggered logging of decay-induced transitions).
        ``now`` is injectable for deterministic testing. Fail-safe.
        """
        try:
            tset = self.get(symbol)
            if tset is None:
                return (False, FLAT, 0.0)
            t = _now_epoch() if now is None else float(now)
            evs, dominant, _ = self._run_challenge(tset, t)
            if dominant not in (LONG, SHORT):
                return (False, FLAT, 0.0)
            ev_over_flat = evs[dominant] - evs[FLAT]
            if ev_over_flat >= self.min_ev_threshold:
                return (True, dominant, ev_over_flat)
            return (False, FLAT, ev_over_flat)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[thesis] should_act({}) ignored a fault: {}", symbol, exc)
            return (False, FLAT, 0.0)

    def get_best_thesis(
        self, symbol: str, now: Optional[float] = None,
    ) -> tuple[str, float, Optional[Thesis]]:
        """The current dominant thesis by *effective* (decayed) EV.

        Returns ``(direction, effective_ev, thesis)`` — ``(FLAT, 0.0, None)``
        when nothing is tracked. Unlike :meth:`should_act` this does not apply
        the opportunity-cost margin; it just reports which side currently leads
        once decay is accounted for. Fail-safe.
        """
        try:
            tset = self.get(symbol)
            if tset is None:
                return (FLAT, 0.0, None)
            t = _now_epoch() if now is None else float(now)
            _, dominant, dominant_ev = self._decayed_view(tset, t)
            return (dominant, dominant_ev, tset.get(dominant))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[thesis] get_best_thesis({}) ignored a fault: {}", symbol, exc)
            return (FLAT, 0.0, None)

    # ── Decay ───────────────────────────────────────────────────────────

    def decay_all(self) -> None:
        """Age every stored thesis one bar — call on each bar-close.

        Directional theses that were not refreshed this bar lose standing:
        ``decay_factor *= decay_rate`` and ``confidence`` is scaled by the new
        factor. The Flat thesis is exempt — doing nothing is a permanent
        baseline, not a fading opinion. Fail-safe.
        """
        try:
            with self._lock:
                for tset in self._sets.values():
                    for thesis in (tset.long_thesis, tset.short_thesis):
                        thesis.decay_factor *= self.decay_rate
                        thesis.confidence = _clamp01(
                            thesis.confidence * self.decay_rate
                        )
                        thesis.age_bars += 1
                    # Re-rank: a decayed directional EV is unchanged here (EV is
                    # evidence-driven, not time-driven) but confidence decay is
                    # surfaced for downstream consumers / the dashboard.
        except Exception as exc:  # noqa: BLE001
            logger.debug("[thesis] decay_all ignored a fault: {}", exc)

    # ── Continuous challenge (decay-triggered transitions) ───────────────

    def challenge(self, symbol: str, now: Optional[float] = None) -> None:
        """Evaluate + log the decay-induced transitions for one symbol.

        Edge-triggered (each transition logs at most once per evidence
        generation) so it is safe to call as often as the symbol is read.
        Fail-safe. See :meth:`_run_challenge` for the transitions surfaced.
        """
        try:
            tset = self.get(symbol)
            if tset is None:
                return
            t = _now_epoch() if now is None else float(now)
            self._run_challenge(tset, t)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[thesis] challenge({}) ignored a fault: {}", symbol, exc)

    def challenge_all(self, now: Optional[float] = None) -> None:
        """Run :meth:`challenge` across every tracked symbol. Fail-safe."""
        try:
            with self._lock:
                sets = list(self._sets.values())
            t = _now_epoch() if now is None else float(now)
            for tset in sets:
                self._run_challenge(tset, t)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[thesis] challenge_all ignored a fault: {}", exc)

    def _run_challenge(
        self, tset: ThesisSet, now: float,
    ) -> tuple[dict[str, float], str, float]:
        """Compute the decayed view and log meaningful transitions once each.

        Three transitions, each edge-triggered via a flag on the ThesisSet (a
        fresh :meth:`update` rebuilds the set and resets them):

        * **Below action threshold** — the side that was dominant at build time
          has decayed so its effective edge over Flat is under
          ``min_ev_threshold``. New entries that way would now be rejected.
        * **Expired** — that side's effective EV has fallen below
          ``decay_floor``: the idea is effectively dead for lack of evidence.
        * **Decay-induced flip** — the decayed dominant is a *different* side
          than the build-time dominant (a fresh opposing thesis overtook a stale
          leader). Distinct from an evidence-induced change.

        Returns the decayed view so callers (``should_act``) can reuse it.
        Fail-safe: on any error returns the decayed view without logging.
        """
        evs, dominant, dominant_ev = self._decayed_view(tset, now)
        try:
            base_dom = tset.dominant
            # Decay-induced dominance flip.
            if dominant != base_dom and not tset.decay_flip_logged:
                logger.info(
                    "[thesis-decay] {} decay-induced dominance flip {}→{} "
                    "(effective EV L={:.4f} S={:.4f} F={:.4f})",
                    tset.symbol, base_dom or "-", dominant,
                    evs[LONG], evs[SHORT], evs[FLAT],
                )
                tset.decay_flip_logged = True

            # Threshold + expiry checks apply to the side that was dominant when
            # the evidence last landed (the thesis a position would lean on).
            if base_dom in (LONG, SHORT):
                base_thesis = tset.get(base_dom)
                eff = evs[base_dom]
                eff_over_flat = eff - evs[FLAT]
                stale_s = self._staleness(base_thesis, now)
                if (
                    eff_over_flat < self.min_ev_threshold
                    and not tset.decay_below_threshold_logged
                ):
                    logger.warning(
                        "[thesis-decay] {} {} thesis decayed below action "
                        "threshold — effective EV over flat {:.4f}R < {:.4f}R "
                        "after {:.0f}s without fresh evidence; new {} entries "
                        "will be rejected until refreshed",
                        tset.symbol, base_dom, eff_over_flat,
                        self.min_ev_threshold, stale_s, base_dom,
                    )
                    tset.decay_below_threshold_logged = True
                if eff < self.decay_floor and not tset.decay_expired_logged:
                    logger.info(
                        "[thesis-decay] {} {} thesis EXPIRED — effective EV "
                        "{:.4f}R below floor {:.4f}R after {:.0f}s without fresh "
                        "evidence (no actionable information remains)",
                        tset.symbol, base_dom, eff, self.decay_floor, stale_s,
                    )
                    tset.decay_expired_logged = True
        except Exception as exc:  # noqa: BLE001 — logging must never break a read
            logger.debug("[thesis] _run_challenge({}) ignored a fault: {}",
                         getattr(tset, "symbol", "?"), exc)
        return evs, dominant, dominant_ev

    @staticmethod
    def _staleness(thesis: Thesis, now: float) -> float:
        """Seconds since ``thesis`` was last refreshed (>= 0)."""
        last = _safe_float(getattr(thesis, "last_refreshed_at", 0.0))
        if last <= 0.0:
            return 0.0
        return max(0.0, now - last)

    # ── Introspection ───────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Summary of all tracked thesis sets for the dashboard / ops.

        EVs are surfaced both as stored base values (each thesis ``to_dict``)
        and as effective, time-decayed values (the ``effective`` block per
        symbol), so an operator can see a thesis quietly losing standing before
        it flips or expires.
        """
        try:
            with self._lock:
                sets = list(self._sets.values())
            now = _now_epoch()
            actionable = 0
            theses_out: dict[str, dict] = {}
            for tset in sets:
                act, _, _ = self._should_act_for(tset, now)
                if act:
                    actionable += 1
                entry = tset.to_dict()
                evs, dominant, dominant_ev = self._decayed_view(tset, now)
                entry["effective"] = {
                    "long_ev": round(evs[LONG], 4),
                    "short_ev": round(evs[SHORT], 4),
                    "flat_ev": round(evs[FLAT], 4),
                    "dominant": dominant,
                    "dominant_ev": round(dominant_ev, 4),
                    "long_decay": round(
                        self._time_decay_factor(tset.long_thesis, now), 4),
                    "short_decay": round(
                        self._time_decay_factor(tset.short_thesis, now), 4),
                }
                theses_out[tset.symbol] = entry
            return {
                "enabled": True,
                "tracked_symbols": len(sets),
                "actionable": actionable,
                "min_ev_threshold": self.min_ev_threshold,
                "decay_rate": self.decay_rate,
                "flat_ev": self.flat_ev,
                "decay_enabled": self.decay_enabled,
                "decay_half_life": self.decay_half_life,
                "decay_floor": self.decay_floor,
                "theses": theses_out,
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[thesis] get_status ignored a fault: {}", exc)
            return {"enabled": True, "tracked_symbols": 0, "theses": {}}

    def _should_act_for(
        self, tset: ThesisSet, now: Optional[float] = None,
    ) -> tuple[bool, str, float]:
        t = _now_epoch() if now is None else float(now)
        evs, dominant, _ = self._decayed_view(tset, t)
        if dominant in (LONG, SHORT):
            ev_over_flat = evs[dominant] - evs[FLAT]
            if ev_over_flat >= self.min_ev_threshold:
                return (True, dominant, ev_over_flat)
        return (False, FLAT, 0.0)

    def reset(self) -> None:
        """Drop all tracked theses (tests / manual ops reset)."""
        with self._lock:
            self._sets.clear()


def _signed_magnitude(vote: Any) -> float:
    """``|weight x confidence|`` for a vote — the magnitude it contributes.

    Prefers a precomputed ``signed`` property when present (e.g.
    :class:`brain.directional_consensus.Vote.signed`); otherwise derives it from
    ``weight`` and ``confidence``. Always non-negative.
    """
    signed = getattr(vote, "signed", None)
    if signed is not None:
        return abs(_safe_float(signed))
    weight = max(0.0, _safe_float(getattr(vote, "weight", 0.0)))
    confidence = _clamp01(_safe_float(getattr(vote, "confidence", 0.0)))
    return weight * confidence


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, _safe_float(value)))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _now_epoch() -> float:
    """Wall-clock epoch seconds — the time-decay clock reference."""
    return time.time()


__all__ = ["Thesis", "ThesisSet", "ThesisEngine", "LONG", "SHORT", "FLAT"]

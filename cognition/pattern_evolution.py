"""APEX TRADER — Pattern evolution (Constitution Article X).

Article X mandates that APEX "must be capable of discovering relationships,
patterns, causal sequences ..." AND evolve: the 15 deterministic rules in
:mod:`cognition.pattern_rules` are a static seed, not the final word. This module
closes that gap. :class:`PatternOutcomeTracker` observes which detected patterns
preceded winning versus losing campaigns and:

* grades each pattern's demonstrated edge (win rate + average R), turning it into
  a bounded *confidence modifier* the Brain multiplies into its conviction —
  patterns that keep losing are attenuated, patterns that keep winning are
  amplified (both bounded, and only once statistically meaningful);
* proposes brand-new structural rules by mining evidence-domain combinations
  that co-occur in winning trades but are not yet expressed by any existing rule
  (Article X — discovering relationships that were never explicitly programmed).

Pure standard library, thread-safe (a single non-reentrant lock), fail-safe
(every public method swallows its own faults so a learning bug can never break a
reasoning cycle) and bounded (FIFO history eviction).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger("apex.cognition.pattern_evolution")


def _clampf(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return min(hi, max(lo, f))


# Domain signatures already expressed by the seed rules in
# :mod:`cognition.pattern_rules` (read off each rule's ``in_domain`` keys). A
# mined domain-combination that matches one of these is NOT a new relationship —
# it is already covered — so :meth:`PatternOutcomeTracker.suggest_new_rules`
# excludes it. Keyed by the pattern name each rule emits.
_EXISTING_DOMAIN_COMBOS: "dict[str, frozenset]" = {
    "liquidity_sweep_absorbed": frozenset({"liquidity"}),
    "liquidity_sweep_with_momentum_exhaustion": frozenset({"liquidity", "momentum"}),
    "displacement_confirmed": frozenset({"structure", "volume"}),
    "momentum_divergence": frozenset({"momentum"}),
    "multi_tf_aligned": frozenset({"multi_timeframe"}),
    "multi_tf_conflict": frozenset({"multi_timeframe"}),
    "volatility_compression": frozenset({"volatility"}),
    "volume_confirmed_breakout": frozenset({"volume", "volatility"}),
    "order_flow_at_structure": frozenset({"order_flow", "structure"}),
    "fvg_liquidity_confluence": frozenset({"order_flow", "liquidity"}),
    "structural_break": frozenset({"structure"}),
    "momentum_acceleration": frozenset({"momentum"}),
    "execution_degraded": frozenset({"execution_quality"}),
    "portfolio_concentrated": frozenset({"portfolio"}),
    "order_flow_imbalance": frozenset({"order_flow"}),
}


def _existing_pattern_names() -> "set[str]":
    """The names of the seed rules (fail-safe). Prefers the live registry so the
    set stays in sync if a rule is added, falling back to the static map."""
    names = set(_EXISTING_DOMAIN_COMBOS)
    try:
        from cognition import pattern_rules as _pr

        for fn in getattr(_pr, "_PATTERN_RULES", []) or []:
            nm = str(getattr(fn, "__name__", "") or "")
            if nm.startswith("rule_"):
                names.add(nm[len("rule_"):])
    except Exception:  # noqa: BLE001 — discovery must never raise
        pass
    return names


def _existing_domain_signatures() -> "set[frozenset]":
    return set(_EXISTING_DOMAIN_COMBOS.values())


class _Record:
    """One pattern origination and its (eventual) realised outcome."""

    __slots__ = ("pattern_name", "symbol", "direction", "confidence",
                 "outcome_won", "pnl_r", "timestamp")

    def __init__(self, pattern_name: str, symbol: str, direction: str,
                 confidence: float, timestamp: float) -> None:
        self.pattern_name = pattern_name
        self.symbol = symbol
        self.direction = direction
        self.confidence = confidence
        self.outcome_won: Optional[bool] = None
        self.pnl_r: Optional[float] = None
        self.timestamp = timestamp


class PatternOutcomeTracker:
    """Learns each structural pattern's demonstrated edge from campaign outcomes.

    Thread-safe, fail-safe, bounded. Neutral (modifier 1.0) until a pattern has
    at least ``min_samples`` resolved outcomes, so a lucky handful cannot move
    the Brain's conviction.
    """

    def __init__(
        self,
        *,
        min_samples: int = 10,
        gain: float = 1.0,
        min_modifier: float = 0.7,
        max_modifier: float = 1.3,
        max_records: int = 2000,
    ) -> None:
        self.min_samples = max(1, int(min_samples))
        self.gain = max(0.0, float(gain))
        self.min_modifier = _clampf(min_modifier, 0.0, 1.0, 0.7)
        self.max_modifier = max(1.0, _clampf(max_modifier, 1.0, 100.0, 1.3))
        self.max_records = max(1, int(max_records))
        self._records: "list[_Record]" = []
        self._lock = threading.Lock()

    # ── recording ─────────────────────────────────────────────────────────

    def record_origination(
        self, pattern_name: str, symbol: str, direction: str,
        confidence: float, timestamp: float,
    ) -> None:
        """Record that a campaign was opened while ``pattern_name`` was detected.

        One campaign can detect multiple patterns; call this once per pattern.
        Fail-safe (never raises)."""
        try:
            name = str(pattern_name or "").strip()
            if not name:
                return
            rec = _Record(
                pattern_name=name,
                symbol=str(symbol or "").strip(),
                direction=str(direction or "").strip().upper(),
                confidence=_clampf(confidence, 0.0, 1.0, 0.0),
                timestamp=float(timestamp) if timestamp is not None else 0.0,
            )
            with self._lock:
                self._records.append(rec)
                # Bounded history — FIFO eviction of the oldest records.
                overflow = len(self._records) - self.max_records
                if overflow > 0:
                    del self._records[:overflow]
        except Exception as exc:  # noqa: BLE001 — learning must never raise
            logger.debug("[pattern-evolution] record_origination fault: %s", exc)

    def record_outcome(
        self, pattern_name: str, symbol: str, won: bool, pnl_r: float,
    ) -> None:
        """Attach a realised outcome to the earliest still-open record matching
        ``(pattern_name, symbol)``. Fail-safe (never raises)."""
        try:
            name = str(pattern_name or "").strip()
            sym = str(symbol or "").strip()
            if not name:
                return
            with self._lock:
                for rec in self._records:
                    if (rec.pattern_name == name and rec.symbol == sym
                            and rec.outcome_won is None):
                        rec.outcome_won = bool(won)
                        rec.pnl_r = _clampf(pnl_r, -1e9, 1e9, 0.0)
                        return
        except Exception as exc:  # noqa: BLE001 — learning must never raise
            logger.debug("[pattern-evolution] record_outcome fault: %s", exc)

    # ── statistics ────────────────────────────────────────────────────────

    def pattern_stats(self, pattern_name: str) -> dict:
        """Demonstrated edge for one pattern (fail-safe, always returns a dict)."""
        empty = {
            "attempts": 0, "wins": 0, "losses": 0, "pending": 0,
            "win_rate": 0.0, "avg_pnl_r": 0.0, "confidence_modifier": 1.0,
        }
        try:
            name = str(pattern_name or "").strip()
            if not name:
                return empty
            with self._lock:
                recs = [r for r in self._records if r.pattern_name == name]
            wins = sum(1 for r in recs if r.outcome_won is True)
            losses = sum(1 for r in recs if r.outcome_won is False)
            pending = sum(1 for r in recs if r.outcome_won is None)
            attempts = wins + losses
            win_rate = (wins / attempts) if attempts > 0 else 0.0
            pnls = [r.pnl_r for r in recs if r.outcome_won is not None and r.pnl_r is not None]
            avg_pnl_r = (sum(pnls) / len(pnls)) if pnls else 0.0
            modifier = self._modifier(attempts, win_rate)
            return {
                "attempts": attempts,
                "wins": wins,
                "losses": losses,
                "pending": pending,
                "win_rate": round(win_rate, 4),
                "avg_pnl_r": round(avg_pnl_r, 4),
                "confidence_modifier": modifier,
            }
        except Exception as exc:  # noqa: BLE001 — stats must never raise
            logger.debug("[pattern-evolution] pattern_stats fault: %s", exc)
            return empty

    def confidence_modifier(self, pattern_name: str) -> float:
        """Bounded multiplicative modifier for a pattern (1.0 until significant)."""
        try:
            return float(self.pattern_stats(pattern_name)["confidence_modifier"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("[pattern-evolution] confidence_modifier fault: %s", exc)
            return 1.0

    def _modifier(self, attempts: int, win_rate: float) -> float:
        if attempts < self.min_samples:
            return 1.0
        raw = 1.0 + self.gain * (win_rate - 0.5)
        return round(min(self.max_modifier, max(self.min_modifier, raw)), 4)

    # ── rule discovery (Article X) ─────────────────────────────────────────

    def suggest_new_rules(
        self, evidence_log: "list[tuple[list[str], str, bool]]",
    ) -> "list[dict]":
        """Mine winning evidence-domain combinations not yet expressed by a rule.

        ``evidence_log`` is a list of ``(evidence_domain_names, direction, won)``
        tuples from closed campaigns. A domain-combination that co-occurred in
        winning trades at >= 60% with >= 5 occurrences, and whose domain
        signature is not already covered by a seed rule, is surfaced as a
        candidate new rule. Fail-safe (always returns a list)."""
        try:
            combos: "dict[frozenset, list[bool]]" = {}
            for entry in evidence_log or []:
                try:
                    domains, _direction, won = entry
                except (TypeError, ValueError):
                    continue
                names = frozenset(
                    str(d or "").strip().lower() for d in (domains or []) if str(d or "").strip()
                )
                if len(names) < 2:
                    # A single-domain "combination" is not a relationship.
                    continue
                combos.setdefault(names, []).append(bool(won))

            existing_names = _existing_pattern_names()
            existing_sigs = _existing_domain_signatures()
            suggestions: "list[dict]" = []
            for domains, outcomes in combos.items():
                occurrences = len(outcomes)
                if occurrences < 5:
                    continue
                win_rate = sum(1 for w in outcomes if w) / occurrences
                if win_rate < 0.60:
                    continue
                if domains in existing_sigs:
                    continue
                suggested_name = "learned_" + "_".join(sorted(domains))
                if suggested_name in existing_names:
                    continue
                suggestions.append({
                    "domains": sorted(domains),
                    "win_rate": round(win_rate, 4),
                    "occurrences": occurrences,
                    "suggested_name": suggested_name,
                })
            suggestions.sort(key=lambda s: (s["win_rate"], s["occurrences"]), reverse=True)
            return suggestions
        except Exception as exc:  # noqa: BLE001 — discovery must never raise
            logger.debug("[pattern-evolution] suggest_new_rules fault: %s", exc)
            return []

    # ── observability ───────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Summary for observability (fail-safe, always returns a dict)."""
        try:
            with self._lock:
                names = sorted({r.pattern_name for r in self._records})
                total = len(self._records)
            ranked = []
            for name in names:
                stats = self.pattern_stats(name)
                if stats["attempts"] > 0:
                    ranked.append((name, stats["win_rate"], stats["attempts"]))
            ranked.sort(key=lambda t: t[1], reverse=True)
            top = [{"pattern": n, "win_rate": wr, "attempts": a} for n, wr, a in ranked[:3]]
            bottom = [{"pattern": n, "win_rate": wr, "attempts": a}
                      for n, wr, a in ranked[-3:]] if ranked else []
            return {
                "total_records": total,
                "patterns_tracked": len(names),
                "min_samples": self.min_samples,
                "top_by_win_rate": top,
                "bottom_by_win_rate": bottom,
            }
        except Exception as exc:  # noqa: BLE001 — status must never raise
            logger.debug("[pattern-evolution] get_status fault: %s", exc)
            return {"total_records": 0, "patterns_tracked": 0, "min_samples": self.min_samples,
                    "top_by_win_rate": [], "bottom_by_win_rate": []}


__all__ = ["PatternOutcomeTracker"]

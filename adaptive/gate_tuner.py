"""
APEX TRADER — Gate Auto-Tuner (shadow-outcome feedback)

Closes the loop on rejected-setup counterfactuals: if a *quality* entry gate's
rejected setups keep WINNING (per the live shadow engine), the gate is too
strict — loosen its threshold a little. If they keep LOSING, tighten back
toward neutral. Every adjustment is small, clamped to a bounded envelope,
persisted, and logged.

SAFETY: only QUALITY gates are tunable. Safety / physical gates
(governor, risk_engine, margin, spread, daily_budget, sidedness, validator)
are NEVER touched — loosening those would risk capital. Intelligence is
learned, not hardcoded; the config values remain the neutral fallback.

Leaf module — standard library + loguru only.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from adaptive.tunable import TuningGuardMixin


class GateTuner(TuningGuardMixin):
    """Learns bounded threshold offsets for quality gates from shadow outcomes."""

    # family -> (min_offset, max_offset, step, loosen_sign)
    #   applied threshold = base + offset
    #   loosen_sign: +1 if loosening means a larger offset, -1 if smaller.
    # Only families listed here are ever adjusted (whitelist = safe-by-default).
    TUNABLE: dict[str, tuple[float, float, float, int]] = {
        # EV gate rejects when ev_estimate < cutoff (base 0.0). Loosening lowers
        # the cutoff (allow slightly-negative EV) within [-0.10, 0.0].
        "ev_gate": (-0.10, 0.0, 0.01, -1),
        # Entry-engine score bar rejects setups scoring below the configured
        # min_entry_score. Loosening lowers that bar by up to 3 points within
        # [-3.0, 0.0] when its rejected setups keep winning. NEVER drops below
        # the watchlist score or the drawdown-mode floor (enforced at the gate).
        "entry_engine": (-3.0, 0.0, 1.0, -1),
    }

    MIN_SAMPLES = 30        # resolved shadows for a gate before tuning it
    LOOSEN_WINRATE = 0.55   # rejected setups winning ≥ this → gate too strict
    TIGHTEN_WINRATE = 0.40  # rejected setups winning ≤ this → gate is right

    def __init__(self, path: str = "data/gate_tuning.json") -> None:
        self._path = Path(path)
        self._offsets: dict[str, float] = {}
        self._load()

    # ── Read API (used by the gates) ─────────────────────────────────────

    def offset(self, family: str) -> float:
        return self._offsets.get(family, 0.0)

    def all_offsets(self) -> dict[str, float]:
        """All tunable-gate offsets (for dashboard/observability)."""
        return {f: self._offsets.get(f, 0.0) for f in self.TUNABLE}

    def threshold(self, family: str, base: float) -> float:
        """Return the learned, bounded threshold for a gate (base + offset)."""
        return base + self._offsets.get(family, 0.0)

    # ── Counterfactual observability (read-only, all gates) ──────────────

    def summarize(self, outcomes_by_gate: list[dict]) -> dict[str, dict]:
        """Per-gate counterfactual stats from shadow outcomes — observability only.

        Unlike `calibrate` (which only touches the whitelisted TUNABLE quality
        gates), this summarises EVERY rejecting-gate family present in the shadow
        outcomes — including the high-authority gates that are never auto-tuned
        (e.g. ``decision_engine``, ``planner``, ``regime_threshold``,
        ``risk_engine``, ``governor``). For each family it reports how often the
        setups it rejected would have won vs lost, so operators can see whether
        the system's real bottlenecks are preserving or suppressing edge. It
        changes no thresholds.

        `outcomes_by_gate` is ShadowStore.get_outcomes_by_gate() — rows of
        {rejecting_gate, outcome, cnt, avg_r}.
        """
        agg: dict[str, dict[str, int]] = {}
        for row in outcomes_by_gate or []:
            family = str(row.get("rejecting_gate", "")).split(":", 1)[0]
            if not family:
                continue
            outcome = row.get("outcome")
            cnt = int(row.get("cnt", 0) or 0)
            d = agg.setdefault(family, {})
            d[outcome] = d.get(outcome, 0) + cnt

        summary: dict[str, dict] = {}
        for family, counts in agg.items():
            wins = counts.get("WIN", 0) + counts.get("PARTIAL", 0)
            losses = counts.get("LOSS", 0)
            total = wins + losses
            summary[family] = {
                "rejected_resolved": total,
                "would_have_won": wins,
                "would_have_lost": losses,
                "would_have_won_rate": round(wins / total, 3) if total else 0.0,
                "auto_tuned": family in self.TUNABLE,
                "offset": round(self._offsets.get(family, 0.0), 4),
            }
        return summary

    # ── Calibration (run periodically from shadow outcomes) ──────────────

    def calibrate(self, outcomes_by_gate: list[dict]) -> list[tuple]:
        """Nudge tunable-gate offsets from shadow outcome stats.

        `outcomes_by_gate` is ShadowStore.get_outcomes_by_gate() — rows of
        {rejecting_gate, outcome, cnt, avg_r}. Returns the list of changes
        (family, old, new, win_rate, samples).
        """
        # Blocked (returns no changes) when the Tuner Agent is sole authority
        # and this is a direct call rather than an agent-driven one.
        if self._tuning_blocked("calibrate"):
            return []
        agg: dict[str, dict[str, int]] = {}
        for row in outcomes_by_gate or []:
            family = str(row.get("rejecting_gate", "")).split(":", 1)[0]
            if family not in self.TUNABLE:
                continue
            outcome = row.get("outcome")
            cnt = int(row.get("cnt", 0) or 0)
            d = agg.setdefault(family, {})
            d[outcome] = d.get(outcome, 0) + cnt

        changed: list[tuple] = []
        for family, counts in agg.items():
            wins = counts.get("WIN", 0) + counts.get("PARTIAL", 0)
            losses = counts.get("LOSS", 0)
            total = wins + losses
            if total < self.MIN_SAMPLES:
                continue
            win_rate = wins / total
            lo, hi, step, sign = self.TUNABLE[family]
            cur = self._offsets.get(family, 0.0)
            new = cur
            if win_rate >= self.LOOSEN_WINRATE:
                new = cur + sign * step          # loosen
            elif win_rate <= self.TIGHTEN_WINRATE:
                new = cur - sign * step          # tighten back toward neutral
            new = round(max(lo, min(hi, new)), 4)
            if abs(new - cur) > 1e-9:
                self._offsets[family] = new
                changed.append((family, cur, new, round(win_rate, 3), total))
                logger.info(
                    "🎛️ Gate tuned — {} offset {:+.3f} → {:+.3f} "
                    "(shadow win-rate {:.0%} over {} resolved)",
                    family, cur, new, win_rate, total,
                )

        if changed:
            self._save()
        return changed

    # ── Persistence (atomic) ─────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._offsets = {
                    str(k): float(v)
                    for k, v in data.items()
                    if k in self.TUNABLE
                }
        except Exception as exc:
            logger.warning("[GateTuner] load failed — using neutral offsets: {}", exc)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from persistence.atomic_write import atomic_write_text

            atomic_write_text(self._path, json.dumps(self._offsets, indent=2))
        except Exception:
            try:
                self._path.write_text(json.dumps(self._offsets, indent=2), encoding="utf-8")
            except Exception as exc:
                logger.warning("[GateTuner] save failed: {}", exc)

"""
APEX TRADER — Outcome Feedback Loop

The orchestrator and the modules upstream of it produce rich, justified
evidence for every entry — but historically nothing closed the loop: after a
trade won or lost, no contributor ever learned whether its read was right.  The
hardcoded confidences (momentum's flat 0.8, the ranker's base win rate) never
met reality.

This module closes that loop.  On entry it records *who drove the trade* — the
opportunity's contributing modules, the horizon, the ranker EV, the
orchestrator's size verdict — keyed by the broker order id.  On close it links
the realised R back to that attribution.  Joining the two halves yields
per-module, per-horizon accuracy and calibration (predicted confidence vs actual
win rate) that the dashboard surfaces and a future phase can use to weight votes
by track record.

It is purely observational — it never changes a live decision.  Storage mirrors
the proven ``planning.outcome_logger`` pattern: append-only JSONL (one record
per line) joined by id, for cheap streaming and crash safety.

Leaf module — standard library + loguru only.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


class OutcomeFeedback:
    """Persists entry-attribution → realised-outcome pairs and aggregates them."""

    def __init__(self, config=None) -> None:
        self._enabled = bool(getattr(config, "enabled", True)) if config is not None else True
        path = getattr(config, "journal_path", "data/outcome_feedback.jsonl") if config is not None else "data/outcome_feedback.jsonl"
        self._lookback = int(getattr(config, "accuracy_lookback", 300)) if config is not None else 300
        # Cap the append-only journal so it can't grow without bound over
        # months of operation (it is fully re-read on every aggregation).
        self._max_records = int(getattr(config, "max_records", 20000)) if config is not None else 20000
        self._rotate_every = 500
        self._append_count = 0
        self._path = Path(path)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("[OutcomeFeedback] could not create journal dir: {}", exc)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── Writing ──────────────────────────────────────────────────────────

    def record_entry(self, trade_key: str, attribution: dict) -> None:
        """Record which modules / opportunity drove a placed trade.

        ``trade_key`` is the broker order id (stable, available at close).
        ``attribution`` carries the contributors, horizon, ranker EV/confidence
        and the orchestrator size verdict.
        """
        if not self._enabled or not trade_key:
            return
        self._append({
            "type": "entry",
            "trade_key": str(trade_key),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "attribution": attribution or {},
        })

    def record_outcome(self, trade_key: str, outcome: dict) -> None:
        """Link a realised outcome (R, P&L, win/loss) back to its entry."""
        if not self._enabled or not trade_key:
            return
        self._append({
            "type": "outcome",
            "trade_key": str(trade_key),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome or {},
        })

    def _append(self, record: dict) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            logger.warning("[OutcomeFeedback] write failed: {}", exc)
            return
        self._append_count += 1
        if self._append_count % self._rotate_every == 0:
            self._maybe_rotate()

    def _maybe_rotate(self) -> None:
        """Trim the journal to the most recent ``_max_records`` lines.

        Rewrites atomically (temp file + os.replace) so a crash mid-rotation
        never corrupts the journal.
        """
        if self._max_records <= 0:
            return
        try:
            if not self._path.exists():
                return
            with self._path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
            if len(lines) <= self._max_records:
                return
            keep = lines[-self._max_records:]
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=self._path.name, suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    tmp.writelines(keep)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, self._path)
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
            logger.info(
                "[OutcomeFeedback] journal rotated — kept last {} of {} records",
                self._max_records, len(lines),
            )
        except Exception as exc:
            logger.warning("[OutcomeFeedback] journal rotation failed: {}", exc)

    # ── Reading / aggregation ──────────────────────────────────────────────

    def _read_records(self) -> tuple[dict, dict, list[str]]:
        entries: dict[str, dict] = {}
        outcomes: dict[str, dict] = {}
        order: list[str] = []
        if not self._path.exists():
            return entries, outcomes, order
        try:
            lines = self._path.read_text(encoding="utf-8").strip().split("\n")
        except Exception as exc:
            logger.warning("[OutcomeFeedback] read failed: {}", exc)
            return entries, outcomes, order
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = rec.get("trade_key")
            if not key:
                continue
            if rec.get("type") == "entry":
                if key not in entries:
                    order.append(key)
                entries[key] = rec
            elif rec.get("type") == "outcome":
                outcomes[key] = rec
        return entries, outcomes, order

    def get_completed(self, lookback: Optional[int] = None) -> list[dict]:
        """Joined entry+outcome pairs (both halves present), newest last."""
        entries, outcomes, order = self._read_records()
        completed: list[dict] = []
        for key in order:
            if key in entries and key in outcomes:
                completed.append({
                    "trade_key": key,
                    "attribution": entries[key].get("attribution", {}),
                    "outcome": outcomes[key].get("outcome", {}),
                })
        lb = self._lookback if lookback is None else lookback
        if lb and lb > 0:
            completed = completed[-lb:]
        return completed

    def module_accuracy(self, lookback: Optional[int] = None) -> dict:
        """Per-module (× horizon) accuracy + calibration over completed trades.

        For every module that contributed to a placed trade, accumulate the
        trade's realised win/loss and R, plus the module's own predicted
        confidence, so the dashboard can show win rate, average R, and how well
        each module's stated confidence matched reality (calibration).
        """
        completed = self.get_completed(lookback)
        modules: dict[str, dict] = {}
        horizons: dict[str, dict] = {}
        total = 0
        wins = 0
        r_sum = 0.0

        for rec in completed:
            attr = rec.get("attribution", {}) or {}
            out = rec.get("outcome", {}) or {}
            pnl_r = _safe_float(out.get("pnl_r"))
            won = bool(out.get("won")) if "won" in out else (pnl_r > 0)
            total += 1
            wins += 1 if won else 0
            r_sum += pnl_r

            horizon = str(attr.get("horizon", "") or "UNKNOWN").upper() or "UNKNOWN"
            h = horizons.setdefault(horizon, {"trades": 0, "wins": 0, "r_sum": 0.0})
            h["trades"] += 1
            h["wins"] += 1 if won else 0
            h["r_sum"] += pnl_r

            votes = attr.get("votes", {}) or {}
            contributors = attr.get("contributors", []) or []
            # Prefer the per-module vote map (carries confidence); fall back to
            # the bare contributor list when only names are available.
            module_names = list(votes.keys()) if votes else list(contributors)
            for name in module_names:
                m = modules.setdefault(
                    name,
                    {"trades": 0, "wins": 0, "r_sum": 0.0, "conf_sum": 0.0, "conf_cnt": 0},
                )
                m["trades"] += 1
                m["wins"] += 1 if won else 0
                m["r_sum"] += pnl_r
                conf = _module_conf(votes.get(name))
                if conf is not None:
                    m["conf_sum"] += conf
                    m["conf_cnt"] += 1

        module_rows = []
        for name, m in modules.items():
            t = m["trades"]
            win_rate = m["wins"] / t if t else 0.0
            avg_conf = m["conf_sum"] / m["conf_cnt"] if m["conf_cnt"] else None
            module_rows.append({
                "module": name,
                "trades": t,
                "wins": m["wins"],
                "win_rate": round(win_rate, 4),
                "avg_r": round(m["r_sum"] / t, 4) if t else 0.0,
                "avg_confidence": round(avg_conf, 4) if avg_conf is not None else None,
                # Calibration gap: predicted confidence − realised win rate.
                # ~0 = well calibrated; >0 = overconfident; <0 = underconfident.
                "calibration_gap": (
                    round(avg_conf - win_rate, 4) if avg_conf is not None else None
                ),
            })
        module_rows.sort(key=lambda r: r["trades"], reverse=True)

        horizon_rows = [
            {
                "horizon": h,
                "trades": v["trades"],
                "wins": v["wins"],
                "win_rate": round(v["wins"] / v["trades"], 4) if v["trades"] else 0.0,
                "avg_r": round(v["r_sum"] / v["trades"], 4) if v["trades"] else 0.0,
            }
            for h, v in horizons.items()
        ]
        horizon_rows.sort(key=lambda r: r["trades"], reverse=True)

        return {
            "modules": module_rows,
            "horizons": horizon_rows,
            "total_trades": total,
            "overall_win_rate": round(wins / total, 4) if total else 0.0,
            "overall_avg_r": round(r_sum / total, 4) if total else 0.0,
        }


def _safe_float(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _module_conf(vote) -> Optional[float]:
    """Extract a module's predicted confidence from its stored vote entry.

    Accepts ``[direction, confidence]`` / ``(direction, confidence)`` or a bare
    confidence float; returns ``None`` when no confidence is available.
    """
    if vote is None:
        return None
    if isinstance(vote, (list, tuple)) and len(vote) >= 2:
        return _safe_float(vote[1])
    if isinstance(vote, (int, float)):
        return float(vote)
    return None

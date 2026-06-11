"""
APEX TRADER — Dashboard Decision Intelligence Mixin
Reads the Decision Journal (JSONL files) and exposes recent decisions,
situation dimension stats, and governor veto counts to the dashboard.
"""

from __future__ import annotations

import json
import os
import time as _time
from pathlib import Path
from typing import Any

from loguru import logger


class DecisionMixin:
    """Reads Decision Journal JSONL files for dashboard display."""

    _JOURNAL_DIR = Path("data/decision_journal")
    _DECISIONS_CACHE_TTL = 3.0

    def get_decisions(
        self,
        limit: int = 50,
        decision_type: str = "",
        symbol: str = "",
    ) -> dict:
        """Recent decisions from the journal with optional filters."""
        now = _time.monotonic()
        cache_key = f"{limit}:{decision_type}:{symbol}"
        cached = getattr(self, "_decisions_cache", None)
        cached_key = getattr(self, "_decisions_cache_key", "")
        cached_ts = getattr(self, "_decisions_cache_ts", 0.0)
        if cached and cached_key == cache_key and (now - cached_ts) < self._DECISIONS_CACHE_TTL:
            return cached

        records = self._read_recent_journal(limit * 3)

        if decision_type:
            dt_upper = decision_type.upper()
            records = [
                r for r in records
                if r.get("decision_type", "MANAGEMENT").upper() == dt_upper
            ]
        if symbol:
            sym_upper = symbol.upper()
            records = [r for r in records if r.get("symbol", "").upper() == sym_upper]

        records = records[:limit]

        stats = self._compute_decision_stats(records)

        result = {
            "decisions": records,
            "stats": stats,
            "total": len(records),
        }
        self._decisions_cache = result
        self._decisions_cache_key = cache_key
        self._decisions_cache_ts = now
        return result

    def get_decision_stats(self) -> dict:
        """Aggregated decision statistics from recent journal entries."""
        records = self._read_recent_journal(500)
        return self._compute_decision_stats(records)

    def _compute_decision_stats(self, records: list[dict]) -> dict:
        action_counts: dict[str, int] = {}
        situation_counts: dict[str, int] = {}
        governor_vetoes = 0
        governor_changes = 0
        total = len(records)

        alignment_sum = 0.0
        structure_sum = 0.0
        momentum_sum = 0.0
        confidence_sum = 0.0
        dim_count = 0

        for r in records:
            action = r.get("decision", {}).get("action", "UNKNOWN")
            action_counts[action] = action_counts.get(action, 0) + 1

            label = r.get("situation", {}).get("label", "UNKNOWN")
            situation_counts[label] = situation_counts.get(label, 0) + 1

            dec = r.get("decision", {})
            if dec.get("governor_changed"):
                governor_changes += 1
            if dec.get("governor_vetoed"):
                governor_vetoes += 1

            sit = r.get("situation", {})
            if "tf_alignment" in sit:
                alignment_sum += sit["tf_alignment"]
                structure_sum += sit.get("structure_integrity", 0)
                momentum_sum += sit.get("momentum", 0)
                confidence_sum += sit.get("read_confidence", 0)
                dim_count += 1

        avg_dims = {}
        if dim_count > 0:
            avg_dims = {
                "avg_tf_alignment": round(alignment_sum / dim_count, 3),
                "avg_structure_integrity": round(structure_sum / dim_count, 3),
                "avg_momentum": round(momentum_sum / dim_count, 3),
                "avg_read_confidence": round(confidence_sum / dim_count, 3),
            }

        return {
            "total_decisions": total,
            "action_counts": action_counts,
            "situation_counts": situation_counts,
            "governor_changes": governor_changes,
            "governor_vetoes": governor_vetoes,
            "dimensions": avg_dims,
        }

    def _read_recent_journal(self, max_records: int = 200) -> list[dict]:
        """Read the most recent JSONL files, newest first."""
        if not self._JOURNAL_DIR.exists():
            return []

        try:
            files = sorted(self._JOURNAL_DIR.glob("decisions_*.jsonl"), reverse=True)
        except Exception as exc:
            logger.debug("[dashboard] journal glob failed: {}", exc)
            return []

        records: list[dict] = []
        for fpath in files[:3]:
            try:
                lines = fpath.read_text(encoding="utf-8").strip().split("\n")
                for line in reversed(lines):
                    if not line.strip():
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if len(records) >= max_records:
                        break
            except Exception as exc:
                logger.debug("[dashboard] journal read failed for {}: {}", fpath.name, exc)
            if len(records) >= max_records:
                break

        return records

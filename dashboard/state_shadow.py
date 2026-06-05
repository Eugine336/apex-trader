"""
APEX TRADER — Dashboard Shadow Outcomes Mixin (Phase 5)
Reads the shadow contract store (apex_shadow.db) for rejected/skipped
setup outcomes — WIN/LOSS/BE/PARTIAL/EXPIRED grouped by rejecting gate.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

from loguru import logger


class ShadowMixin:
    """Reads from ShadowStore for rejected-setup outcome tracking."""

    def _get_shadow_store(self):
        try:
            from persistence.shadow_store import ShadowStore, _DB_PATH
            if not hasattr(self, "_shadow_store_inst"):
                self._shadow_store_inst = ShadowStore()
            return self._shadow_store_inst
        except Exception as e:
            logger.debug(f"Shadow store unavailable: {e}")
            return None

    def get_shadow_outcomes(self) -> dict:
        """Rejected/skipped setup outcomes grouped by rejecting gate."""
        store = self._get_shadow_store()
        if store is None:
            return {"gates": [], "summary": {}, "contracts": []}

        try:
            raw = store.get_outcomes_by_gate()
            status_counts = store.count_by_status()

            gates: Dict[str, Dict[str, Any]] = {}
            for row in raw:
                gate = row["rejecting_gate"]
                outcome = row["outcome"]
                cnt = row["cnt"]
                avg_r = row.get("avg_r")
                if gate not in gates:
                    gates[gate] = {
                        "gate": gate,
                        "total": 0,
                        "WIN": 0, "LOSS": 0, "EXPIRED": 0,
                        "BE": 0, "PARTIAL": 0,
                        "avg_r": 0.0,
                    }
                gates[gate][outcome] = cnt
                gates[gate]["total"] += cnt
                if avg_r is not None and outcome in ("WIN", "LOSS"):
                    gates[gate]["avg_r"] = round(avg_r, 2)

            gate_list = sorted(gates.values(), key=lambda g: g["total"], reverse=True)
            for g in gate_list:
                total = g["total"]
                if total > 0:
                    g["win_rate"] = round(g["WIN"] / total * 100, 1)
                else:
                    g["win_rate"] = 0.0

            recent = store.get_all_contracts(limit=50)
            contracts = []
            for c in recent:
                contracts.append({
                    "contract_id": c.contract_id,
                    "correlation_id": c.correlation_id,
                    "symbol": c.symbol,
                    "direction": c.direction,
                    "entry_price": c.entry_price,
                    "stop_loss": c.stop_loss,
                    "tp1": c.tp1,
                    "tp2": c.tp2,
                    "rejecting_gate": c.rejecting_gate,
                    "score": c.score,
                    "status": c.status,
                    "outcome": c.outcome,
                    "r_multiple": c.r_multiple,
                    "exit_reason": c.exit_reason,
                    "exit_price": c.exit_price,
                    "resolution_granularity": c.resolution_granularity,
                    "bars_replayed": c.bars_replayed,
                    "timestamp": c.ts_utc_ms,
                })

            return {
                "gates": gate_list,
                "summary": status_counts,
                "contracts": contracts,
            }
        except Exception as exc:
            logger.debug("[state_shadow] get_shadow_outcomes failed: {}", exc)
            return {"gates": [], "summary": {}, "contracts": []}

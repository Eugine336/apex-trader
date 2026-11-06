"""Full-context execution-cost reading for the native Apex Brain."""
from __future__ import annotations

from typing import Any

from cognition.concepts.base import ConceptReading
from cognition.contracts import EvidenceDomain


class ExecutionCostReadings:
    key = "execution_cost"
    family = "execution"
    covers_methods = ("execution_cost",)
    covers_lenses: tuple[str, ...] = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        # Execution costs are broker/event observations, not candle patterns.
        return []

    def read_with_context(
        self,
        symbol: str,
        bars_by_tf: dict[str, list[dict]],
        context: dict[str, Any],
    ) -> list[ConceptReading]:
        payload = context.get("execution_cost") if isinstance(context, dict) else None
        if not isinstance(payload, dict):
            return []
        source_epoch = None
        try:
            source_epoch = float(payload.get("source_timestamp_epoch"))
        except (TypeError, ValueError, OverflowError):
            pass
        quote_verified = bool(
            payload.get("source_timestamp_verified") is True
            and source_epoch is not None and source_epoch > 0.0
        )
        unknowns_raw = payload.get("cost_unknown_components", ())
        unknowns = tuple(
            str(item)[:200] for item in unknowns_raw if str(item or "").strip()
        ) if isinstance(unknowns_raw, (list, tuple)) else ()
        complete = payload.get("cost_verified") is True and payload.get("cost_components_complete") is True
        status = "verified_complete" if complete else (
            "partial_with_verified_quote" if quote_verified else "source_quote_unverified"
        )
        return [ConceptReading(
            concept="execution_cost",
            timeframe="SPEC",
            domain=EvidenceDomain.EXECUTION_QUALITY,
            applicable=quote_verified,
            applicability_reason=(
                "A fresh source-attested executable bid/ask quote is available for broker-cost analysis."
                if quote_verified else
                "A fresh source-attested executable bid/ask quote is unavailable; cost conclusions remain provisional."
            ),
            observation=(
                f"{symbol}: broker execution-cost analysis is {status}; "
                "spread, fees, financing, slippage and fill telemetry remain separately attributed."
            ),
            implications=(),
            facts=dict(payload),
            source_bar_epoch=None,
            unknowns=unknowns,
            data_basis="broker_quote_contract_spec_and_execution_attempt_history",
            independence_group=f"execution_cost:{str(symbol or '').strip().upper()}",
            relevance_horizon_seconds=10.0,
            source_timestamp_epoch=source_epoch if quote_verified else None,
            source_timestamp_verified=quote_verified,
        )]

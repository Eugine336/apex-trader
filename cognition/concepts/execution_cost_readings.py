"""Full-context execution-cost reading for the native Apex Brain."""
from __future__ import annotations

import math
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
        # SAFE-FALLBACK: malformed provider time remains unknown and cannot authorize exposure.
        except (TypeError, ValueError, OverflowError):
            pass
        quote_verified = bool(
            payload.get("source_timestamp_verified") is True
            and source_epoch is not None
            and math.isfinite(source_epoch)
            and source_epoch > 0.0
        )
        unknowns_raw = payload.get("cost_unknown_components", ())
        unknowns = tuple(
            str(item)[:200] for item in unknowns_raw if str(item or "").strip()
        ) if isinstance(unknowns_raw, (list, tuple)) else ()
        complete = payload.get("cost_verified") is True and payload.get("cost_components_complete") is True
        status = "verified_complete" if complete else (
            "partial_with_verified_quote" if quote_verified else "source_quote_unverified"
        )
        facts = dict(payload)
        quote = payload.get("quote")
        quote_spread = (
            quote.get("spread") if isinstance(quote, dict) else None
        )
        sample_floor_met = payload.get("market_entry_fill_rate_sample_floor_met") is True
        # Short canonical measurement names satisfy the analytical registry,
        # while explicit aliases retain units and whether data are entry-only.
        facts.update({
            "spread": quote_spread,
            "spread_account_ccy_per_lot": payload.get(
                "estimated_spread_account_ccy_per_lot"
            ),
            "commission": payload.get("estimated_commission_account_ccy_per_lot"),
            "slippage": payload.get("observed_entry_abs_slippage_avg_pips"),
            "slippage_scope": "market_entry_only",
            "round_trip_slippage_account_ccy_per_lot": payload.get(
                "estimated_round_trip_slippage_account_ccy_per_lot"
            ),
            "swap": payload.get("estimated_financing_account_ccy_per_lot"),
            "latency": payload.get("observed_submit_response_latency_avg_ms"),
            "fill_probability": (
                payload.get("market_entry_fill_success_ratio")
                if sample_floor_met else None
            ),
            "fill_probability_semantics": (
                "observed market-entry success ratio after minimum sample floor; "
                "not a predictive probability"
            ),
        })
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
            facts=facts,
            source_bar_epoch=None,
            unknowns=unknowns,
            data_basis="broker_quote_contract_spec_and_execution_attempt_history",
            independence_group=f"execution_cost:{str(symbol or '').strip().upper()}",
            relevance_horizon_seconds=10.0,
            source_timestamp_epoch=source_epoch if quote_verified else None,
            source_timestamp_verified=quote_verified,
            evidence_confidence=0.8 if quote_verified else 0.0,
            evidence_uncertainty=0.2 if quote_verified else 1.0,
        )]

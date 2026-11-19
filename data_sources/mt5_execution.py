"""Read exact MT5 deals against the quote captured for the acknowledged request."""

from __future__ import annotations

import math
import time

from data_sources.contracts import (
    CapabilityState, Completeness, DataObservation, ObservationKind,
    ObservationProvenance, SourceIdentity,
)
from platforms.mt5.fill_benchmark import matched_fill_epoch


class MT5ExecutionCostAdapter:
    provider_id = "mt5-execution"

    def __init__(self, manager, store_source=None):
        self._manager = manager
        if store_source is None:
            from persistence.event_store import get_existing_event_store
            store_source = get_existing_event_store
        self._store_source = store_source

    def capabilities(self):
        return (CapabilityState(
            provider=self.provider_id, capability=ObservationKind.EXECUTION_COST,
            completeness=Completeness.UNKNOWN, sample_count=0,
            reason="Requires a captured market-entry benchmark and its exact MT5 history deal",
        ),)

    def fetch(self, kind, *, canonical_instrument=None, start_epoch=None, end_epoch=None):
        if kind is not ObservationKind.EXECUTION_COST or not canonical_instrument or end_epoch is None:
            return ()
        store = self._store_source()
        if store is None:
            return ()
        receipts = store.recent_fill_benchmarks(canonical_instrument)
        if not receipts:
            return ()
        connector = self._manager.get_connector(canonical_instrument)
        lookup = getattr(connector, "get_benchmark_deal", None)
        if not callable(lookup):
            return ()
        # Duplicate delivery of the same acknowledgement is harmless. A conflicting
        # benchmark for the same broker deal makes that sample indeterminate.
        unique = {}
        for item in receipts:
            key = (item["account_scope"], item["deal_ticket"])
            if key in unique and unique[key] != item:
                unique[key] = None
            else:
                unique[key] = item
        observations = []
        for (scope, deal_id), receipt in unique.items():
            if receipt is None or receipt["submit_end_utc"] > end_epoch:
                continue
            deal = lookup(canonical_instrument, receipt)
            if deal is None:
                continue
            resolved = matched_fill_epoch(receipt, deal["time_msc"])
            if resolved is None:
                continue
            fill_epoch, domain = resolved
            bid, ask = receipt["quote_bid"], receipt["quote_ask"]
            volume, price = deal["volume"], deal["price"]
            quote_epoch = receipt["quote_timestamp_epoch"]
            if (
                not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                        for v in (volume, price, bid, ask, quote_epoch))
                or not (0 < bid <= ask and volume > 0 and price > 0 and 0 < quote_epoch <= fill_epoch <= end_epoch)
                or (start_epoch is not None and fill_epoch < start_epoch)
            ):
                continue
            observations.append(DataObservation(
                provenance=ObservationProvenance(
                    source=SourceIdentity(self.provider_id, "mt5", canonical_instrument, receipt["native_symbol"]),
                    kind=kind, source_timestamp_epoch=fill_epoch, source_timestamp_verified=True,
                    received_timestamp_epoch=time.time(), completeness=Completeness.PARTIAL,
                    source_record_id=f"{scope}:{deal_id}",
                ),
                values={
                    "side": receipt["side"], "executed_size": volume, "fill_price": price,
                    "quote_bid": bid, "quote_ask": ask, "quote_timestamp_epoch": quote_epoch,
                    "fill_timestamp_epoch": fill_epoch, "commission_account_currency": deal["commission"],
                    "commission_currency": receipt["commission_currency"],
                    "benchmark": "actual_market_submit_quote",
                    "raw_fill_time_msc": deal["time_msc"], "raw_quote_time_msc": receipt["quote_raw_time_msc"],
                    "fill_time_domain": domain, "utc_uncertainty_seconds": receipt["utc_uncertainty_seconds"],
                    "broker_offset_seconds": receipt["broker_offset_seconds"],
                },
            ))
        return tuple(observations)

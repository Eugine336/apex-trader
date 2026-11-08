"""Explicit unknown-preserving readers for concepts whose external feeds are absent.

These readers make the missing input contract visible to the Brain. They do not
infer external facts from OHLC, emit directional hypotheses, or authorize trades.
When a provider channel is implemented, its dedicated reader should replace the
corresponding gate rather than silently changing this module's meaning.
"""
from __future__ import annotations

from typing import Any

from cognition.concepts.auction_utils import clean_bars, last_source_epoch
from cognition.concepts.base import FLAT, ConceptReading, Implication, reference_price
from cognition.contracts import EvidenceDomain


_DATA_GATES: dict[str, tuple[EvidenceDomain, str]] = {
    "smt_divergence": (
        EvidenceDomain.CORRELATION,
        "synchronized completed bars for a measured correlated-instrument pair, including verified common-cutoff source timestamps and confirmed swing/sweep events",
    ),
    "cross_asset": (
        EvidenceDomain.CORRELATION,
        "source-verified multi-symbol completed bars aligned to one decision cutoff, with stable instrument identity and sufficient paired history",
    ),
    "currency_strength": (
        EvidenceDomain.CORRELATION,
        "source-verified synchronized bars for the enabled currency universe and verified base/quote currency metadata",
    ),
    "relative_value": (
        EvidenceDomain.CORRELATION,
        "source-verified synchronized histories for the candidate pair plus independently tested spread stationarity and relationship stability",
    ),
    "event_driven": (
        EvidenceDomain.MACRO,
        "trusted timestamped economic-event calendar with actual, forecast and previous values, currency/instrument relevance and release-time provenance",
    ),
    "macro_rates": (
        EvidenceDomain.MACRO,
        "source-verified policy-rate paths, real yields, yield curves and inflation series with observation and publication timestamps",
    ),
    "macro_growth": (
        EvidenceDomain.MACRO,
        "trusted GDP, CPI, PCE, employment and PMI event records with actual/forecast/previous values and source timestamps",
    ),
    "carry": (
        EvidenceDomain.EXECUTION_QUALITY,
        "verified direction-specific broker swap or funding rates, contract units, rollover schedule and financing-day rules for the exact instrument",
    ),
    "derivatives": (
        EvidenceDomain.OTHER,
        "verified instrument expiry/roll specifications and provider-sourced options implied volatility, skew and open-interest data where applicable",
    ),
    "crypto_microstructure": (
        EvidenceDomain.ORDER_FLOW,
        "source-verified exchange funding, open interest, liquidation and basis observations with exact venue, instrument and timestamps",
    ),
    "synthetic_market": (
        EvidenceDomain.OTHER,
        "provider-verified synthetic contract specification and source-timestamped instrument history sufficient to measure provider-specific behaviour",
    ),
    "delta_cvd": (
        EvidenceDomain.ORDER_FLOW,
        "executed trade volume with verified aggressor-side classification and source timestamps; quote-tick inference must be labelled as an estimate",
    ),
    "footprint": (
        EvidenceDomain.ORDER_FLOW,
        "trade-level executed volume-at-price with verified aggressor-side volume and provider/source timestamps",
    ),
    "absorption_exhaustion": (
        EvidenceDomain.ORDER_FLOW,
        "source-verified executed volume and aggressor-side flow at price levels, plus replenishment/depletion or a clearly labelled broker depth proxy",
    ),
    "market_impact": (
        EvidenceDomain.EXECUTION_QUALITY,
        "attributable broker fill history keyed by instrument, side and executed size, with contemporaneous quote, fill, commission and slippage timestamps",
    ),
}


DATA_GATED_METHODS = {
    key: requirement for key, (_domain, requirement) in _DATA_GATES.items()
}


class ExternalDataGatedReadings:
    """Emit explicit non-directional readings for unavailable external feeds."""

    key = "external_data_gated_readings"
    family = "external_data_prerequisites"
    covers_methods: tuple[str, ...] = ()
    covers_lenses: tuple[str, ...] = ()
    data_gated_methods = DATA_GATED_METHODS

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        timeframe = reference_tf or ""
        bars = clean_bars((bars_by_tf or {}).get(timeframe, []))
        if not timeframe or not bars:
            return []
        epoch = last_source_epoch(bars)
        source_verified = bool(
            epoch is not None and bars[-1].get("source_timestamp_verified") is True
        )
        readings: list[ConceptReading] = []
        for concept, (domain, requirement) in _DATA_GATES.items():
            readings.append(ConceptReading(
                concept=concept,
                timeframe=timeframe,
                domain=domain,
                applicable=True,
                applicability_reason=(
                    f"{concept} is relevant knowledge, but its required external data channel "
                    "is not supplied to this reader."
                ),
                observation=(
                    f"{symbol} {timeframe}: {concept} remains data-gated; "
                    "no external value or directional conclusion was inferred from OHLC."
                ),
                implications=(Implication(
                    direction=FLAT,
                    statement=(
                        f"{concept} cannot be interpreted until its required source data is available."
                    ),
                    strength=None,
                    activated=False,
                    activation_reason="Required external feed is unavailable or not integrated.",
                    requires=(requirement,),
                    conflicts_with=(
                        "Do not infer the missing external feed from price-only observations.",
                    ),
                    management={
                        "meaning": "Context only; no direction, entry, sizing, exit or execution authority.",
                    },
                ),),
                facts={
                    "data_gated": True,
                    "data_gated_reason": requirement,
                    "external_feed_observed": False,
                    "directional_decision_emitted": False,
                    "entry_authorization_emitted": False,
                },
                source_bar_epoch=epoch,
                reference_close=reference,
                reference_timeframe=reference_tf,
                unknowns=(requirement,),
                data_basis="completed_ohlc",
                independence_group=f"ohlc:{str(symbol or '').upper()}:{timeframe}",
                source_timestamp_epoch=epoch if source_verified else None,
                source_timestamp_verified=source_verified,
            ))

        return readings

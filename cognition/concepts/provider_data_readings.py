"""Read provider-neutral external observations into native Brain concept readings.

Concrete vendor adapters are deliberately separate. This module accepts only
normalized DataObservation records from DataSourceRegistry and preserves unknowns
when required fields, timestamps, publication vintages or source identities fail.
"""
from __future__ import annotations

import math
from typing import Any

from cognition.concepts.base import FLAT, ConceptReading, Implication, reference_price
from cognition.concepts.auction_utils import last_source_epoch
from cognition.contracts import EvidenceDomain
from data_sources import DataObservation, ObservationKind


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _verified(
    observation: Any, kind: ObservationKind, cutoff: float,
) -> bool:
    if not isinstance(observation, DataObservation):
        return False
    provenance = observation.provenance
    stamp = _number(provenance.source_timestamp_epoch)
    return bool(
        provenance.kind is kind
        and provenance.source_timestamp_verified is True
        and stamp is not None and stamp > 0
        and (kind is ObservationKind.ECONOMIC_EVENT or stamp <= cutoff)
        and provenance.source.provider.strip()
        and provenance.source.venue.strip()
        and provenance.source.canonical_instrument.strip()
        and provenance.source.native_instrument.strip()
        and provenance.source_record_id
    )


def _make(
    concept: str, symbol: str, timeframe: str, epoch: float,
    reference: float | None, domain: EvidenceDomain, observation: str,
    facts: dict[str, Any], unknowns: tuple[str, ...] = (),
    *, provider_epoch: float | None = None,
) -> ConceptReading:
    return ConceptReading(
        concept=concept, timeframe=timeframe, domain=domain,
        applicable=True,
        applicability_reason=f"{concept} is read from normalized, source-attributed provider observations.",
        observation=observation,
        implications=(Implication(
            direction=FLAT,
            statement="Provider observations are context for the Brain, not a directional vote or execution instruction.",
            strength=None, activated=False,
            activation_reason="The provider adapter supplies observations, not trade decisions.",
            requires=unknowns,
            conflicts_with=("Never substitute receipt time, price proxies or another venue's records for the declared source.",),
            management={"meaning": "Contextual evidence only; Brain retains decision authority."},
        ),),
        facts=facts, source_bar_epoch=epoch, reference_close=reference,
        reference_timeframe=timeframe, unknowns=unknowns,
        data_basis="normalized_provider_observations",
        independence_group=f"provider_data:{concept}:{symbol}",
        source_timestamp_epoch=provider_epoch,
        source_timestamp_verified=provider_epoch is not None,
    )


class ProviderDataReadings:
    key = "provider_data_readings"
    family = "provider_observations"
    covers_methods: tuple[str, ...] = ()
    covers_lenses: tuple[str, ...] = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        return []

    def read_with_context(
        self, symbol: str, bars_by_tf: dict[str, list[dict]], context: dict[str, Any],
    ) -> list[ConceptReading]:
        registry = context.get("data_source_registry")
        cutoff = _number(
            context.get("analysis_epoch", context.get("multi_symbol_snapshot_cutoff_epoch"))
        )
        if registry is None or cutoff is None:
            return []
        reference, timeframe = reference_price(bars_by_tf)
        if not timeframe:
            return []
        target_bars = bars_by_tf.get(timeframe, [])
        bar_epoch = last_source_epoch(target_bars)
        if bar_epoch is None:
            return []
        output: list[ConceptReading] = []
        provider_errors: dict[str, str] = {}

        def fetch(kind: ObservationKind, instrument: str | None = None) -> list[DataObservation]:
            try:
                if callable(getattr(registry, "fetch_with_status", None)):
                    result, errors = registry.fetch_with_status(
                        kind, canonical_instrument=instrument,
                    )
                    for provider, error in errors.items():
                        provider_errors[f"{kind.value}:{provider}"] = error
                else:
                    result = registry.fetch(kind, canonical_instrument=instrument)
                return [item for item in result if _verified(item, kind, cutoff)]
            except Exception as exc:  # noqa: BLE001 — preserve other provider channels
                provider_errors[kind.value] = type(exc).__name__
                return []

        symbol_spec = context.get("symbol_spec")
        symbol_spec = symbol_spec if isinstance(symbol_spec, dict) else {}
        symbol_spec_verified = bool(
            symbol_spec.get("source_verified") is True
            and symbol_spec.get("specification_verified") is True
        )
        relevant_currencies = {
            str(symbol_spec.get(key) or "").strip().upper()
            for key in ("currency_base", "currency_profit", "currency_margin")
            if str(symbol_spec.get(key) or "").strip()
        } if symbol_spec_verified else set()

        # Economic calendar: event time and publication/revision vintages are separate.
        events = []
        for item in fetch(ObservationKind.ECONOMIC_EVENT):
            values, provenance = item.values, item.provenance
            publication = _number(provenance.publication_timestamp_epoch)
            revision = _number(provenance.revision_timestamp_epoch)
            event_time = _number(values.get("event_time_epoch", provenance.source_timestamp_epoch))
            currency = str(values.get("currency") or "").strip().upper()
            if (
                publication is None or publication > cutoff
                or (revision is not None and revision > cutoff)
                or event_time is None or not currency
                or (relevant_currencies and currency not in relevant_currencies)
            ):
                continue
            events.append({
                "provider": provenance.source.provider,
                "venue": provenance.source.venue,
                "record_id": provenance.source_record_id,
                "event_id": str(values.get("event_id") or provenance.source_record_id),
                "event_time_epoch": event_time,
                "publication_timestamp_epoch": publication,
                "revision_timestamp_epoch": revision,
                "currency": currency,
                "category": str(values.get("event_category") or "").strip().lower(),
                "importance": values.get("importance"),
                "actual": _number(values.get("actual")),
                "forecast": _number(values.get("forecast")),
                "previous": _number(values.get("previous")),
            })
        events.sort(key=lambda row: row["event_time_epoch"])
        if events:
            event_unknowns = tuple(
                sorted({
                    field for event in events
                    for field in ("actual", "forecast", "previous")
                    if event[field] is None
                })
            )
            output.append(_make(
                "event_driven", symbol, timeframe, bar_epoch, reference,
                EvidenceDomain.MACRO,
                f"{symbol}: {len(events)} source-attributed economic events are available for relevant currencies.",
                {"events": events, "event_count": len(events),
                 "provider_errors": dict(provider_errors),
                 "vintage_rule": "publication and revision timestamps must be no later than the analysis cutoff"},
                tuple(f"event records missing {field} values" for field in event_unknowns),
                provider_epoch=max(event["publication_timestamp_epoch"] for event in events),
            ))
            growth = [event for event in events if event["category"] in {
                "gdp", "cpi", "pce", "employment", "payrolls", "pmi", "inflation",
            }]
            if growth:
                output.append(_make(
                    "macro_growth", symbol, timeframe, bar_epoch, reference,
                    EvidenceDomain.MACRO,
                    f"{symbol}: {len(growth)} relevant growth, inflation or employment release records are available.",
                    {"releases": growth, "release_count": len(growth),
                     "provider_errors": dict(provider_errors)},
                    ("historical point-in-time release vintages and measured instrument reaction history",),
                    provider_epoch=max(event["publication_timestamp_epoch"] for event in growth),
                ))

        # Macro series: observation dates and publication dates remain distinct.
        series = []
        for item in fetch(ObservationKind.MACRO_SERIES):
            values, provenance = item.values, item.provenance
            publication = _number(provenance.publication_timestamp_epoch)
            observation_epoch = _number(values.get("observation_epoch", provenance.source_timestamp_epoch))
            currency = str(values.get("currency") or "").strip().upper()
            if (
                publication is None or publication > cutoff
                or observation_epoch is None or observation_epoch > cutoff
                or not symbol_spec_verified or not relevant_currencies
                or (currency and currency not in relevant_currencies)
                or _number(values.get("value")) is None
                or (provenance.revision_timestamp_epoch is not None
                    and (_number(provenance.revision_timestamp_epoch) is None
                         or provenance.revision_timestamp_epoch > cutoff))
            ):
                continue
            series.append({
                "provider": provenance.source.provider,
                "record_id": provenance.source_record_id,
                "series_id": str(values.get("series_id") or ""),
                "series_family": str(values.get("series_family") or "").strip().lower(),
                "currency": currency or None,
                "observation_epoch": observation_epoch,
                "publication_timestamp_epoch": publication,
                "revision_timestamp_epoch": _number(provenance.revision_timestamp_epoch),
                "value": _number(values.get("value")),
                "unit": values.get("unit"),
            })
        rates = [row for row in series if row["series_family"] in {
            "policy_rate", "real_yield", "yield_curve", "inflation",
        }]
        if rates:
            output.append(_make(
                "macro_rates", symbol, timeframe, bar_epoch, reference,
                EvidenceDomain.MACRO,
                f"{symbol}: {len(rates)} point-in-time rates, yields or inflation observations are available.",
                {"series": rates, "series_count": len(rates),
                 "provider_errors": dict(provider_errors)},
                ("historical macro vintages and measured instrument co-movement",),
                provider_epoch=max(row["publication_timestamp_epoch"] for row in rates),
            ))

        # Derivatives: exact venue and native contract remain attached to every record.
        derivative_rows = []
        for item in fetch(ObservationKind.DERIVATIVES, symbol):
            values, provenance = item.values, item.provenance
            derivative_rows.append({
                "provider": provenance.source.provider, "venue": provenance.source.venue,
                "native_instrument": provenance.source.native_instrument,
                "record_id": provenance.source_record_id,
                "expiry_epoch": _number(values.get("expiry_epoch")),
                "implied_volatility": _number(values.get("implied_volatility")),
                "skew": _number(values.get("skew")),
                "open_interest": _number(values.get("open_interest")),
                "source_timestamp_epoch": provenance.source_timestamp_epoch,
            })
        if derivative_rows:
            output.append(_make(
                "derivatives", symbol, timeframe, bar_epoch, reference,
                EvidenceDomain.OTHER,
                f"{symbol}: {len(derivative_rows)} venue-specific derivatives observations are available.",
                {"contracts": derivative_rows, "contract_count": len(derivative_rows)},
                ("contract expiry and roll conventions verified for the underlying instrument",)
                + tuple(
                    f"derivatives observation missing {field}"
                    for field in ("expiry_epoch", "implied_volatility", "skew", "open_interest")
                    if not any(row[field] is not None for row in derivative_rows)
                ),
                provider_epoch=max(row["source_timestamp_epoch"] for row in derivative_rows),
            ))

        # Crypto metrics are venue/instrument-specific and never combined across venues.
        crypto_rows = []
        for kind in (ObservationKind.FUNDING, ObservationKind.OPEN_INTEREST, ObservationKind.LIQUIDATION):
            for item in fetch(kind, symbol):
                crypto_rows.append({
                    "kind": kind.value, "provider": item.provenance.source.provider,
                    "venue": item.provenance.source.venue,
                    "native_instrument": item.provenance.source.native_instrument,
                    "record_id": item.provenance.source_record_id,
                    "source_timestamp_epoch": item.provenance.source_timestamp_epoch,
                    "values": dict(item.values),
                })
        if crypto_rows:
            output.append(_make(
                "crypto_microstructure", symbol, timeframe, bar_epoch, reference,
                EvidenceDomain.ORDER_FLOW,
                f"{symbol}: {len(crypto_rows)} venue-specific crypto derivatives observations are available.",
                {"observations": crypto_rows, "observation_count": len(crypto_rows)},
                ("venue-specific instrument and contract semantics validated",)
                + tuple(
                    f"venue feed has no {kind.value} observations"
                    for kind in (ObservationKind.FUNDING, ObservationKind.OPEN_INTEREST, ObservationKind.LIQUIDATION)
                    if not any(row["kind"] == kind.value for row in crypto_rows)
                ),
                provider_epoch=max(row["source_timestamp_epoch"] for row in crypto_rows),
            ))

        # True trade prints only: quote ticks and DOM are not relabelled as executed volume.
        prints = []
        for item in fetch(ObservationKind.TRADE_PRINT, symbol):
            values, provenance = item.values, item.provenance
            price, quantity = _number(values.get("price")), _number(values.get("quantity"))
            side = str(values.get("aggressor_side") or "").strip().lower()
            if (
                price is None or price <= 0 or quantity is None or quantity <= 0
                or side not in {"buy", "sell"}
                or provenance.volume_basis != "executed_trade_volume"
                or provenance.aggressor_side_basis != "exchange_classified"
                or provenance.source_timestamp_epoch < bar_epoch
            ):
                continue
            prints.append({
                "price": price, "quantity": quantity, "aggressor_side": side,
                "source_timestamp_epoch": provenance.source_timestamp_epoch,
                "provider": provenance.source.provider, "venue": provenance.source.venue,
                "record_id": provenance.source_record_id,
            })
        if prints:
            delta = sum(
                row["quantity"] if row["aggressor_side"] == "buy" else -row["quantity"]
                for row in prints
            )
            by_price: dict[float, dict[str, float]] = {}
            for row in prints:
                bucket = by_price.setdefault(row["price"], {"buy": 0.0, "sell": 0.0})
                bucket[row["aggressor_side"]] += row["quantity"]
            profile = [
                {"price": price, "buy_volume": values["buy"],
                 "sell_volume": values["sell"], "total_volume": values["buy"] + values["sell"]}
                for price, values in sorted(by_price.items())
            ]
            output.append(_make(
                "delta_cvd", symbol, timeframe, bar_epoch, reference,
                EvidenceDomain.ORDER_FLOW,
                f"{symbol}: exchange-classified executed trade prints provide signed volume for the current completed-bar interval.",
                {"signed_delta": delta, "trade_count": len(prints),
                 "volume_basis": "executed_trade_volume",
                 "aggressor_side_basis": "exchange_classified",
                 "source_venues": sorted({row["venue"] for row in prints})},
                provider_epoch=max(row["source_timestamp_epoch"] for row in prints),
            ))
            output.append(_make(
                "footprint", symbol, timeframe, bar_epoch, reference,
                EvidenceDomain.ORDER_FLOW,
                f"{symbol}: executed volume is attributed to exact traded prices.",
                {"volume_at_price": profile, "price_level_count": len(profile),
                 "trade_count": len(prints), "volume_basis": "executed_trade_volume"},
                provider_epoch=max(row["source_timestamp_epoch"] for row in prints),
            ))

        # Broker-confirmed fills and contemporaneous quotes: empirical market impact,
        # never a predictive fill probability from a thin sample.
        execution_rows = []
        for item in fetch(ObservationKind.EXECUTION_COST, symbol):
            values, provenance = item.values, item.provenance
            side = str(values.get("side") or "").strip().upper()
            size = _number(values.get("executed_size"))
            bid, ask = _number(values.get("quote_bid")), _number(values.get("quote_ask"))
            fill = _number(values.get("fill_price"))
            quote_epoch = _number(values.get("quote_timestamp_epoch"))
            fill_epoch = _number(values.get("fill_timestamp_epoch", provenance.source_timestamp_epoch))
            commission = _number(values.get("commission_account_currency"))
            commission_currency = str(values.get("commission_currency") or "").strip().upper()
            if (
                side not in {"BUY", "SELL"} or size is None or size <= 0
                or bid is None or ask is None or bid <= 0 or ask < bid
                or fill is None or fill <= 0
                or quote_epoch is None or fill_epoch is None
                or quote_epoch > fill_epoch or fill_epoch > cutoff
                or not math.isclose(fill_epoch, provenance.source_timestamp_epoch, rel_tol=0.0, abs_tol=1e-6)
            ):
                continue
            executable_quote = ask if side == "BUY" else bid
            execution_rows.append({
                "provider": provenance.source.provider, "venue": provenance.source.venue,
                "record_id": provenance.source_record_id, "side": side,
                "executed_size": size, "quote_bid": bid, "quote_ask": ask,
                "fill_price": fill, "executable_quote": executable_quote,
                "signed_price_impact": (fill - executable_quote) if side == "BUY" else (executable_quote - fill),
                "quote_timestamp_epoch": quote_epoch, "fill_timestamp_epoch": fill_epoch,
                "commission_account_currency": commission,
                "commission_currency": commission_currency or None,
            })
        if execution_rows:
            missing_costs = any(
                row["commission_account_currency"] is None
                or not row["commission_currency"]
                for row in execution_rows
            )
            output.append(_make(
                "market_impact", symbol, timeframe, bar_epoch, reference,
                EvidenceDomain.EXECUTION_QUALITY,
                f"{symbol}: {len(execution_rows)} broker-confirmed executions are paired with contemporaneous executable quotes.",
                {"executions": execution_rows, "execution_count": len(execution_rows),
                 "mean_signed_price_impact": sum(row["signed_price_impact"] for row in execution_rows) / len(execution_rows),
                 "commission_complete": not missing_costs,
                 "predictive_fill_probability_emitted": False},
                ("selection-complete samples across intended order sizes, sides, volatility states and sessions",)
                + (("complete commission amount and account-currency conversion",) if missing_costs else ()),
                provider_epoch=max(row["fill_timestamp_epoch"] for row in execution_rows),
            ))

        return output

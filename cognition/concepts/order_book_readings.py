"""Broker Level-2 order-book measurements for the native Brain.

A book snapshot supports displayed depth, spread, imbalance and top-level
microprice calculations. It does not reveal hidden liquidity, queue priority,
cancellations, spoofing, or the age of the provider's last book update.
"""
from __future__ import annotations

import time
from typing import Any

from cognition.concepts.base import ConceptReading, finite, reference_price
from cognition.contracts import EvidenceDomain


def _imbalance(bid_size: float, ask_size: float) -> float | None:
    total = bid_size + ask_size
    if total <= 0:
        return None
    return (bid_size - ask_size) / total


def _levels(raw_levels: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, set[str]]:
    bids_by_price: dict[float, float] = {}
    asks_by_price: dict[float, float] = {}
    invalid_count = 0
    volume_bases: set[str] = set()
    if not isinstance(raw_levels, (list, tuple)):
        return [], [], 0, volume_bases
    for raw in raw_levels:
        if not isinstance(raw, dict):
            invalid_count += 1
            continue
        price = finite(raw.get("price"))
        volume = finite(raw.get("volume"))
        side = str(raw.get("side") or "").strip().lower()
        if price is None or price <= 0 or volume is None or volume < 0 or side not in {"bid", "ask"}:
            invalid_count += 1
            continue
        volume_bases.add(str(raw.get("volume_basis") or "unspecified").strip().lower())
        target = bids_by_price if side == "bid" else asks_by_price
        target[price] = target.get(price, 0.0) + volume
    bids = [{"price": p, "volume": v} for p, v in sorted(bids_by_price.items(), reverse=True)]
    asks = [{"price": p, "volume": v} for p, v in sorted(asks_by_price.items())]
    return bids, asks, invalid_count, volume_bases


def _provider_time_known(snapshot: dict[str, Any]) -> bool:
    """Local receipt time is not a provider-originated update time."""
    return (
        finite(snapshot.get("source_timestamp_epoch")) is not None
        or bool(snapshot.get("provider_sequence"))
    )


class OrderBookReadings:
    key = "order_book_readings"
    family = "microstructure"
    covers_methods = ("order_book",)
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        return self.read_with_context(symbol, bars_by_tf, {})

    def read_with_context(
        self,
        symbol: str,
        bars_by_tf: dict[str, list[dict]],
        context: dict[str, Any],
    ) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        raw = context.get("market_depth") if isinstance(context, dict) else None
        snapshot = raw if isinstance(raw, dict) else {}
        raw_levels = snapshot.get("levels")
        source = str(snapshot.get("source") or "")
        source_symbol = str(snapshot.get("source_symbol") or "")
        source_verified = (
            snapshot.get("source_verified") is True
            and bool(source.strip())
            and bool(source_symbol)
            and isinstance(raw_levels, (list, tuple))
            and bool(raw_levels)
            and all(
                isinstance(level, dict)
                and level.get("source_verified") is True
                and str(level.get("source") or "") == source
                and str(level.get("source_symbol") or "") == source_symbol
                for level in raw_levels
            )
        )
        bids, asks, invalid_count, volume_bases = _levels(raw_levels)
        unknowns: list[str] = []
        if not source_verified:
            unknowns.append("source-verified broker Level-2 bid/ask snapshot")
        if not bids or not asks:
            unknowns.append("at least one valid displayed bid level and one valid displayed ask level")
        if invalid_count:
            unknowns.append(f"{invalid_count} invalid or unclassified book level(s) were excluded")
        if len(volume_bases) > 1:
            unknowns.append("consistent displayed-size units across the returned book levels")
        if not _provider_time_known(snapshot):
            unknowns.append(
                "provider-originated book update timestamp or sequence; local retrieval time is not provider source time"
            )
        unknowns.extend([
            "individual order queue priority and the account's position in each queue",
            "order additions, cancellations and replenishment between book snapshots",
            "hidden, iceberg and off-venue liquidity",
            "spoofing or intent behind displayed size",
        ])

        best_bid = bids[0] if bids else None
        best_ask = asks[0] if asks else None
        locked_or_crossed = bool(
            best_bid is not None and best_ask is not None
            and best_bid["price"] >= best_ask["price"]
        )
        if locked_or_crossed:
            unknowns.append("valid uncrossed two-sided quote required for spread and microprice")

        spread = (
            best_ask["price"] - best_bid["price"]
            if best_bid is not None and best_ask is not None and not locked_or_crossed
            else None
        )
        mid = (best_bid["price"] + best_ask["price"]) / 2.0 if spread is not None else None
        spec = context.get("symbol_spec") if isinstance(context, dict) else None
        spec = spec if isinstance(spec, dict) else {}
        tick_size = (
            finite(spec.get("trade_tick_size"))
            if spec.get("source_verified") is True and spec.get("specification_verified") is True
            else None
        )
        spread_ticks = spread / tick_size if spread is not None and tick_size is not None and tick_size > 0 else None
        if spread is not None and spread_ticks is None:
            unknowns.append("verified broker tick size for expressing displayed spread in ticks")

        bid_top = best_bid["volume"] if best_bid is not None else None
        ask_top = best_ask["volume"] if best_ask is not None else None
        queue_imbalance = _imbalance(bid_top, ask_top) if bid_top is not None and ask_top is not None else None
        microprice = (
            (best_ask["price"] * bid_top + best_bid["price"] * ask_top) / (bid_top + ask_top)
            if spread is not None and bid_top is not None and ask_top is not None and bid_top + ask_top > 0
            else None
        )

        depth_by_levels: dict[str, dict[str, Any]] = {}
        for count in (1, 3, 5, 10):
            bid_depth = sum(level["volume"] for level in bids[:count])
            ask_depth = sum(level["volume"] for level in asks[:count])
            depth_by_levels[str(count)] = {
                "requested_levels_per_side": count,
                "bid_levels_observed": min(count, len(bids)),
                "ask_levels_observed": min(count, len(asks)),
                "bid_size": bid_depth,
                "ask_size": ask_depth,
                "combined_size": bid_depth + ask_depth,
                "imbalance": _imbalance(bid_depth, ask_depth),
                "complete_for_requested_level_count": len(bids) >= count and len(asks) >= count,
            }
        top5 = depth_by_levels["5"]
        bid_total = sum(level["volume"] for level in bids)
        ask_total = sum(level["volume"] for level in asks)
        total_imbalance = _imbalance(bid_total, ask_total)
        received_epoch = finite(snapshot.get("snapshot_received_epoch"))
        received_age = max(0.0, time.time() - received_epoch) if received_epoch and received_epoch > 0 else None
        facts = {
            "status": (
                "unavailable_or_unverified" if not source_verified
                else "locked_or_crossed_book" if locked_or_crossed
                else "verified_two_sided_snapshot"
            ),
            "source": source or None,
            "source_symbol": source_symbol or None,
            "source_verified": source_verified,
            "provider_timestamp_verified": False,
            "snapshot_received_epoch": received_epoch,
            "snapshot_received_age_seconds": received_age,
            "volume_bases": sorted(volume_bases),
            "valid_bid_levels": len(bids),
            "valid_ask_levels": len(asks),
            "invalid_levels_excluded": invalid_count,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": mid,
            "spread_price_units": spread,
            "spread_ticks": spread_ticks,
            "top_of_book": {
                "bid_size": bid_top,
                "ask_size": ask_top,
                "displayed_queue_imbalance": queue_imbalance,
                "microprice": microprice,
                "microprice_minus_mid": microprice - mid if microprice is not None and mid is not None else None,
            },
            "depth_by_levels_per_side": depth_by_levels,
            "top5_order_book_imbalance": top5["imbalance"],
            "total_visible_bid_size": bid_total,
            "total_visible_ask_size": ask_total,
            "total_visible_depth_imbalance": total_imbalance,
            "visible_levels": {"bids": bids[:10], "asks": asks[:10]},
            "locked_or_crossed": locked_or_crossed,
            "displayed_size_is_not_executed_volume": True,
            "displayed_queue_imbalance_is_not_account_queue_position": True,
            "provider_update_age_known": False,
            "no_directional_decision_emitted": True,
            "execution_authorization_emitted": False,
        }
        applicable = source_verified and bool(bids) and bool(asks) and not locked_or_crossed
        observation = (
            f"{symbol}: broker order-book snapshot has {len(bids)} valid bid levels and "
            f"{len(asks)} valid ask levels; "
            + (
                f"spread={spread:.8g} price units and top-five displayed imbalance={top5['imbalance']}."
                if applicable else "two-sided depth/spread interpretation is unavailable or invalid."
            )
            + " This is displayed liquidity, not a directional verdict or execution permission."
        )
        return [ConceptReading(
            concept="order_book",
            timeframe="BOOK",
            domain=EvidenceDomain.ORDER_FLOW,
            applicable=applicable,
            applicability_reason=(
                "A source-attested, uncrossed two-sided broker Level-2 snapshot supports displayed depth and imbalance measurements."
                if applicable else "A verified, valid two-sided Level-2 snapshot is not available."
            ),
            observation=observation,
            implications=(),
            facts=facts,
            source_bar_epoch=None,
            reference_close=reference,
            reference_timeframe=reference_tf,
            unknowns=tuple(dict.fromkeys(unknowns)),
            data_basis="broker_order_book_snapshot",
            independence_group=f"broker_order_book:{source or 'unavailable'}:{symbol.upper()}",
            relevance_horizon_seconds=2.0,
        )]

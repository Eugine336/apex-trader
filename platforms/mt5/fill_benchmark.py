"""Point-in-time evidence for an acknowledged MT5 market fill."""

from __future__ import annotations

import hashlib
import math
from typing import Any

from platforms.mt5.clock import CLOCK_MAX_AGE_SECONDS, CLOCK_MAX_UNCERTAINTY_SECONDS


def account_scope(account: Any) -> str | None:
    server = str(getattr(account, "server", "") or "").strip()
    login = getattr(account, "login", None)
    if not server or type(login) is not int or login <= 0:
        return None
    # A deal ticket is unique only on its venue/account. Do not export the login.
    return hashlib.sha256(f"mt5\0{server}\0{login}".encode()).hexdigest()


def capture_fill_benchmark(
    *, account, clock, tick, native_symbol, direction, result, submitted_price,
    submit_monotonic, response_monotonic, max_tick_age_seconds,
) -> dict | None:
    """Capture the quote actually used by the successful submit, including a requote."""
    scope = account_scope(account)
    if scope is None or str(getattr(account, "server", "")) != getattr(clock, "server", None):
        return None
    try:
        anchor = float(clock.observed_monotonic)
        uncertainty = float(clock.uncertainty_seconds)
        offset = float(clock.offset_seconds)
        start = float(clock.observed_gmt_epoch) + (submit_monotonic - anchor)
        end = start + (response_monotonic - submit_monotonic)
        bid, ask = float(tick.bid), float(tick.ask)
        msc, seconds = tick.time_msc, tick.time
        order, deal = result.order, result.deal
        if not all(math.isfinite(value) for value in (start, end, anchor, uncertainty, offset, bid, ask)):
            return None
        if (
            type(msc) is not int or type(seconds) is not int or msc <= 0 or msc // 1000 != seconds
            or type(order) is not int or type(deal) is not int or order <= 0 or deal <= 0
            or not 0 <= uncertainty <= CLOCK_MAX_UNCERTAINTY_SECONDS
            or not 0 <= submit_monotonic - anchor <= response_monotonic - anchor <= CLOCK_MAX_AGE_SECONDS
            or not 0 < start <= end or not 0 < bid <= ask
            or direction not in {"BUY", "SELL"}
            or submitted_price != (ask if direction == "BUY" else bid)
        ):
            return None
        quote_epoch = msc / 1000.0 - offset
        if not 0 <= start - quote_epoch <= max_tick_age_seconds:
            return None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return {
        "account_scope": scope, "native_symbol": native_symbol, "side": direction,
        "order_ticket": str(order), "deal_ticket": str(deal),
        "quote_bid": bid, "quote_ask": ask, "quote_timestamp_epoch": quote_epoch,
        "quote_raw_time_msc": msc, "broker_offset_seconds": offset,
        "submit_start_utc": start, "submit_end_utc": end,
        "utc_uncertainty_seconds": uncertainty,
        "commission_currency": str(getattr(account, "currency", "") or "").strip().upper(),
    }


def matched_fill_epoch(receipt: dict, deal_time_msc: int) -> tuple[float, str] | None:
    """Exactly one interpretation of the raw deal time must fit the witnessed submit."""
    start = float(receipt["submit_start_utc"])
    end = float(receipt["submit_end_utc"])
    uncertainty = float(receipt["utc_uncertainty_seconds"])
    offset = float(receipt["broker_offset_seconds"])
    if not (0 < start <= end and 0 <= uncertainty <= CLOCK_MAX_UNCERTAINTY_SECONDS):
        return None
    # A tick and a deal can have different timestamp domains. Anchor this exact deal
    # to the UTC submit/response interval, independently of the tick convention.
    raw_epoch = deal_time_msc / 1000.0
    candidates = [(raw_epoch, "utc")]
    if offset != 0:
        candidates.append((raw_epoch - offset, "broker_server"))
    matches = [
        (epoch, domain) for epoch, domain in candidates
        if start - uncertainty <= epoch <= end + uncertainty
    ]
    return matches[0] if len(matches) == 1 else None

"""Adapter-origin quote timestamp provenance checks for calibration evidence.

This is an application-side adapter attestation, not a cryptographic broker
signature. It prevents a bare source_timestamp_verified boolean from being
mistaken for a complete, source-specific quote provenance record.
"""
from __future__ import annotations

import math
from typing import Any


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def verify_quote_source_provenance(
    raw_fields: Any,
    *,
    symbol: str,
    quote_kind: str,
    timestamp_epoch: Any,
) -> bool:
    """Validate adapter-specific raw source-time evidence for a live quote."""
    if not isinstance(raw_fields, dict):
        return False
    sym = str(symbol or "").strip().upper()
    kind = str(quote_kind or "").strip().lower()
    epoch = _finite(timestamp_epoch)
    if not sym or epoch is None or epoch <= 0.0:
        return False

    adapter = str(raw_fields.get("source_adapter", "") or "").strip().lower()
    if adapter == "mt5":
        if kind != "bid_ask":
            return False
        utc_epoch = _finite(raw_fields.get("timestamp_utc_epoch"))
        uncertainty = _finite(
            raw_fields.get("source_utc_offset_uncertainty_seconds")
        )
        sources = raw_fields.get("source_clock_sources")
        source_time = _finite(raw_fields.get("time_msc"))
        if source_time is None or source_time <= 0.0:
            source_time = _finite(raw_fields.get("time"))
        server_epoch = (
            source_time / 1000.0
            if source_time is not None and source_time > 10_000_000_000
            else source_time
        )
        offset_seconds = _finite(raw_fields.get("source_utc_offset_seconds"))
        normalized_epoch = (
            server_epoch - offset_seconds
            if server_epoch is not None and offset_seconds is not None
            else None
        )
        return bool(
            str(raw_fields.get("source_timezone", "") or "").lower()
                == "broker_server"
            and utc_epoch is not None
            and abs(utc_epoch - epoch) <= 1e-6
            and uncertainty is not None
            and 0.0 <= uncertainty <= 2.5
            and normalized_epoch is not None
            and abs(normalized_epoch - utc_epoch) <= max(0.05, uncertainty)
            and isinstance(sources, list)
            and len({str(item).strip() for item in sources if str(item).strip()}) >= 2
            and server_epoch is not None
            and server_epoch > 0.0
        )

    if adapter == "deriv":
        if kind != "mid_only":
            return False
        provider_epoch = _finite(raw_fields.get("provider_timestamp_epoch"))
        received_epoch = _finite(raw_fields.get("received_at_epoch"))
        return bool(
            raw_fields.get("provider_timestamp_field") == "history.times"
            and raw_fields.get("provider_timestamp_semantics") == "unix_utc_seconds"
            and provider_epoch is not None
            and abs(provider_epoch - epoch) <= 1e-6
            and received_epoch is not None
            and received_epoch > 0.0
            and received_epoch >= epoch - 1.0
        )

    return False


__all__ = ["verify_quote_source_provenance"]

"""Canonical, tamper-evident manifests for broker historical quote batches.

The digest binds the adapter's query metadata and selected quote to the durable
calibration outcome. It detects payload drift; it is not a broker digital
signature and must not be described as external cryptographic attestation.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def quote_history_quote_digest(quote: dict[str, Any]) -> str:
    """Digest one normalized bid/ask observation."""
    if not isinstance(quote, dict):
        raise TypeError("quote must be a mapping")
    return _canonical_digest(quote)


def build_quote_history_manifest(fields: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic batch ID and manifest digest from adapter fields."""
    if not isinstance(fields, dict):
        raise TypeError("manifest fields must be a mapping")
    body = dict(fields)
    body.pop("manifest_digest", None)
    body.pop("batch_id", None)
    digest = _canonical_digest(body)
    symbol = str(body.get("symbol", "") or "").strip().upper()
    broker = str(body.get("source_broker", "") or "").strip()
    try:
        start = float(body.get("query_window_start_epoch"))
        end = float(body.get("query_window_end_epoch"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("manifest query window is invalid") from exc
    if (
        not symbol or not broker or not math.isfinite(start) or not math.isfinite(end)
        or start <= 0.0 or end < start
    ):
        raise ValueError("manifest identity or query window is invalid")
    batch_id = hashlib.sha256(
        f"{symbol}|{broker}|{start:.6f}|{end:.6f}|{digest}".encode("utf-8")
    ).hexdigest()
    return {**body, "manifest_digest": digest, "batch_id": batch_id}


def verify_quote_history_manifest(manifest: dict[str, Any]) -> bool:
    """Verify adapter boundary, manifest digest, batch ID and core query fields."""
    if not isinstance(manifest, dict):
        return False
    if (
        manifest.get("schema_version") != "1.0.0"
        or manifest.get("source_module") != "platform_manager.quote_history"
        or manifest.get("source") != "mt5.copy_ticks_range"
        or manifest.get("source_verified") is not True
        or manifest.get("source_timezone") != "UTC"
        or manifest.get("query_complete") not in (True, False)
        or manifest.get("selection_complete") not in (True, False)
        or not str(manifest.get("source_broker", "") or "").strip()
        or not str(manifest.get("source_symbol", "") or "").strip()
    ):
        return False
    try:
        start = float(manifest.get("query_window_start_epoch"))
        end = float(manifest.get("query_window_end_epoch"))
        through = float(manifest.get("queried_through_epoch"))
        completed = float(manifest.get("query_completed_at_epoch"))
        tick_count = int(manifest.get("tick_count"))
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        not all(math.isfinite(v) for v in (start, end, through, completed))
        or start <= 0.0 or end < start or through < start
        or through > end + 1e-6 or completed <= 0.0 or tick_count < 0
        or (manifest.get("query_complete") is True and through < end - 1e-6)
    ):
        return False
    selected_digest = manifest.get("selected_quote_digest")
    selected_epoch = manifest.get("selected_quote_epoch")
    if manifest.get("selection_complete") is True:
        try:
            selected_epoch = float(selected_epoch)
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            not isinstance(selected_digest, str) or len(selected_digest) != 64
            or not math.isfinite(selected_epoch)
            or not start <= selected_epoch <= through + 1e-6
        ):
            return False
    elif selected_digest is not None or selected_epoch is not None:
        return False
    expected = build_quote_history_manifest(manifest)
    return (
        manifest.get("manifest_digest") == expected["manifest_digest"]
        and manifest.get("batch_id") == expected["batch_id"]
    )


__all__ = [
    "build_quote_history_manifest",
    "quote_history_quote_digest",
    "verify_quote_history_manifest",
]

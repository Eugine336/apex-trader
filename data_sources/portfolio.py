"""Fail-closed assembly of a broker-confirmed whole-portfolio snapshot."""
from __future__ import annotations

from typing import Any

from data_sources.contracts import Completeness, DataObservation, ObservationKind


def build_portfolio_risk_snapshot(
    registry: Any,
    *,
    analysis_epoch: float,
    completed_bar_epoch: float | None,
) -> dict[str, Any]:
    """Read one atomic, broker-attested snapshot; never synthesize portfolio history."""
    result: dict[str, Any] = {
        "complete": False,
        "source_timestamp_verified": False,
        "confirmed_platforms": [],
        "failed_platforms": [],
        "positions": [],
        "portfolio_returns": [],
        "equity_curve": [],
        "correlation_pairs": [],
        "provider_errors": {},
        "unknowns": [],
    }
    if registry is None:
        result["unknowns"].append("no broker portfolio snapshot adapter is registered")
        return result
    try:
        if callable(getattr(registry, "fetch_with_status", None)):
            observations, errors = registry.fetch_with_status(ObservationKind.PORTFOLIO_SNAPSHOT)
        else:
            observations = registry.fetch(ObservationKind.PORTFOLIO_SNAPSHOT)
            errors = {}
    except Exception as exc:  # noqa: BLE001 — broker snapshot failure remains visible
        result["provider_errors"]["portfolio_snapshot"] = type(exc).__name__
        result["unknowns"].append("broker portfolio snapshot query failed")
        return result
    result["provider_errors"] = dict(errors)
    valid = [
        item for item in observations
        if isinstance(item, DataObservation)
        and item.provenance.kind is ObservationKind.PORTFOLIO_SNAPSHOT
        and item.provenance.source_timestamp_verified is True
        and item.provenance.source_timestamp_epoch is not None
        and item.provenance.source_timestamp_epoch <= analysis_epoch
        and item.provenance.completeness is Completeness.COMPLETE
    ]
    partial = [
        item for item in observations
        if isinstance(item, DataObservation)
        and item.provenance.kind is ObservationKind.PORTFOLIO_SNAPSHOT
        and item.provenance.completeness is Completeness.PARTIAL
    ]
    if len(valid) != 1:
        result["unknowns"].append(
            "exactly one complete, source-verified whole-portfolio snapshot is required; "
            f"received {len(valid)} valid snapshot(s)"
        )
        if errors:
            result["unknowns"].append("one or more portfolio snapshot providers failed")
        if len(partial) == 1:
            result["partial_observation"] = dict(partial[0].values)
            result["partial_source_provider"] = partial[0].provenance.source.provider
            result["unknowns"].extend(
                str(reason) for reason in partial[0].values.get("unknowns", ())
                if isinstance(reason, str) and reason
            )
        elif len(partial) > 1:
            result["unknowns"].append("multiple partial whole-portfolio observations cannot be combined safely")
        return result
    item = valid[0]
    values = dict(item.values)
    stamp = float(item.provenance.source_timestamp_epoch)
    portfolio_id = str(values.get("portfolio_id") or "").strip()
    if not portfolio_id or portfolio_id != item.provenance.source.native_instrument:
        result["unknowns"].append(
            "portfolio identity in snapshot values must match the provider-attested native account identity"
        )
    result.update(values)
    result.update({
        "portfolio_id": portfolio_id,
        "source_timestamp_epoch": stamp,
        "source_timestamp_verified": True,
        "source_provider": item.provenance.source.provider,
        "source_venue": item.provenance.source.venue,
        "complete": bool(values.get("complete") is True and not errors and portfolio_id == item.provenance.source.native_instrument),
        "provider_errors": dict(errors),
    })
    if completed_bar_epoch is not None and stamp < completed_bar_epoch:
        result["complete"] = False
        result["unknowns"] = list(result.get("unknowns", [])) + [
            "portfolio snapshot predates the completed market-bar cutoff"
        ]
    required_lists = ("positions", "portfolio_returns", "equity_curve", "correlation_pairs")
    for field in required_lists:
        if not isinstance(result.get(field), list):
            result["complete"] = False
            result.setdefault("unknowns", []).append(f"portfolio snapshot missing a valid {field} list")
            result[field] = []
    if not result["portfolio_id"]:
        result["complete"] = False
        result.setdefault("unknowns", []).append("portfolio identity is missing")
    if errors:
        result["complete"] = False
        result.setdefault("unknowns", []).append("portfolio snapshot provider failures remain unresolved")
    return result


__all__ = ["build_portfolio_risk_snapshot"]

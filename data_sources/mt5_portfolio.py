"""Read-only MT5 account observations; deliberately never portfolio-complete.

MT5 account and position snapshots do not provide synchronized historical
portfolio returns, equity curves, or pairwise correlations. This adapter makes
the live facts visible to the Brain while preserving those unknowns.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from data_sources.contracts import (
    CapabilityState, Completeness, DataObservation, ObservationKind,
    ObservationProvenance, SourceIdentity,
)


_ACCOUNT_FIELDS = ("balance", "equity", "margin", "margin_free", "margin_level", "currency", "leverage")
_POSITION_FIELDS = ("symbol", "type", "volume", "price_open", "price_current", "sl", "tp", "profit", "swap", "time", "time_msc")


class MT5PortfolioAdapter:
    """Normalize connected MT5 account and position reads without claiming completeness."""

    provider_id = "mt5-portfolio"

    def __init__(self, platform_manager: Any) -> None:
        self._manager = platform_manager

    def capabilities(self) -> Sequence[CapabilityState]:
        connected = sum(bool(value) for value in getattr(self._manager, "_mt5_connected_flags", ()))
        return (CapabilityState(
            provider=self.provider_id,
            capability=ObservationKind.PORTFOLIO_SNAPSHOT,
            completeness=Completeness.PARTIAL if connected else Completeness.UNKNOWN,
            sample_count=connected,
            reason=(
                "MT5 exposes current account and positions only; synchronized verified portfolio histories are absent"
                if connected else "no connected MT5 account is available"
            ),
        ),)

    def fetch(
        self,
        kind: ObservationKind,
        *,
        canonical_instrument: str | None = None,
        start_epoch: float | None = None,
        end_epoch: float | None = None,
    ) -> Sequence[DataObservation]:
        del canonical_instrument, start_epoch, end_epoch
        if kind is not ObservationKind.PORTFOLIO_SNAPSHOT:
            return ()
        connectors = tuple(getattr(self._manager, "mt5_connectors", ()))
        flags = tuple(getattr(self._manager, "_mt5_connected_flags", ()))
        connected = [(i, c) for i, c in enumerate(connectors) if i < len(flags) and flags[i]]
        if not connected:
            return ()

        accounts: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, connector in connected:
            try:
                account = connector.get_account_snapshot()
                if not isinstance(account, dict) or account.get("source_verified") is not True:
                    errors.append(f"connector_{index}:account_unverified")
                    continue
                accounts.append({key: account[key] for key in _ACCOUNT_FIELDS if key in account})
                raw_positions = connector.get_position_snapshots()
                if not isinstance(raw_positions, list):
                    errors.append(f"connector_{index}:positions_invalid")
                    continue
                for row in raw_positions:
                    if isinstance(row, dict):
                        positions.append({key: row[key] for key in _POSITION_FIELDS if key in row})
                    else:
                        errors.append(f"connector_{index}:position_invalid")
            except Exception as exc:  # noqa: BLE001 — partial evidence is retained with a typed error
                errors.append(f"connector_{index}:{type(exc).__name__}")

        if not accounts:
            return ()
        received = datetime.now(timezone.utc).timestamp()
        values = {
            "complete": False,
            "account_observations": accounts,
            "positions_observed": positions,
            "connected_connector_count": len(connected),
            "account_observation_count": len(accounts),
            "observation_errors": errors,
            "unknowns": [
                "MT5 does not provide a verified source timestamp for this combined read",
                "synchronized historical portfolio returns, equity curve, and pairwise correlations are unavailable",
                "per-position account-currency risk and exposure conversions are not provided by this read path",
            ],
        }
        return (DataObservation(
            provenance=ObservationProvenance(
                source=SourceIdentity(
                    provider=self.provider_id,
                    venue="mt5",
                    canonical_instrument="PORTFOLIO",
                    native_instrument="connected-mt5-connectors",
                ),
                kind=kind,
                source_timestamp_epoch=None,
                received_timestamp_epoch=received,
                source_timestamp_verified=False,
                completeness=Completeness.PARTIAL,
                metadata={"read_only": True, "account_identifiers_redacted": True},
            ),
            values=values,
        ),)


__all__ = ["MT5PortfolioAdapter"]

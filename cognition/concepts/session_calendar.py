"""Verified venue session-calendar reading; never substitutes static UTC windows."""
from __future__ import annotations

import logging
import math
from cognition.concepts.auction_utils import clean_bars, last_source_epoch, session_info
from cognition.concepts.base import FLAT, ConceptReading, Implication, reference_price
from cognition.contracts import EvidenceDomain
from data_sources import DataObservation, ObservationKind


class SessionCalendar:
    key = "session_calendar"
    family = "session"
    covers_methods = ()
    covers_lenses = ()
    data_gated_methods = {
        "session_calendar": "authoritative venue calendar with timezone, DST, holiday, session open/close and rollover metadata"
    }

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        return self.read_with_context(symbol, bars_by_tf, {})

    def read_with_context(
        self, symbol: str, bars_by_tf: dict[str, list[dict]], context: dict,
    ) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        raw_spec = context.get("symbol_spec") if isinstance(context, dict) else None
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        broker_sessions = spec.get("broker_trading_sessions")
        broker_schedule_verified = bool(
            spec.get("source_verified") is True
            and spec.get("specification_verified") is True
            and isinstance(broker_sessions, list)
            and broker_sessions
            and all(
                isinstance(item, dict)
                and item.get("start_seconds_server_time") is not None
                and item.get("end_seconds_server_time") is not None
                for item in broker_sessions
            )
        )
        readings = []
        registry = context.get("data_source_registry") if isinstance(context, dict) else None
        cutoff = context.get("analysis_epoch", context.get("multi_symbol_snapshot_cutoff_epoch")) if isinstance(context, dict) else None
        try:
            cutoff = float(cutoff)
            if not math.isfinite(cutoff) or cutoff <= 0:
                cutoff = None
        except (TypeError, ValueError, OverflowError):  # SAFE-FALLBACK: malformed optional analysis clock keeps calendar timing unknown
            cutoff = None
        provider_sessions: list[dict] = []
        provider_calendar_errors: dict[str, str] = {}
        if registry is not None and cutoff is not None:
            try:
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                if callable(getattr(registry, "fetch_with_status", None)):
                    session_rows, provider_calendar_errors = registry.fetch_with_status(
                        ObservationKind.SESSION, canonical_instrument=str(symbol or "").upper(),
                    )
                else:
                    session_rows = registry.fetch(
                        ObservationKind.SESSION, canonical_instrument=str(symbol or "").upper(),
                    )
                for item in session_rows:
                    if not isinstance(item, DataObservation):
                        continue
                    provenance, values = item.provenance, item.values
                    stamp = provenance.source_timestamp_epoch
                    source_identity = provenance.source
                    received_at = provenance.received_timestamp_epoch
                    publication_at = provenance.publication_timestamp_epoch
                    revision_at = provenance.revision_timestamp_epoch
                    if (
                        provenance.kind is not ObservationKind.SESSION
                        or str(source_identity.canonical_instrument or "").strip().upper()
                            != str(symbol or "").strip().upper()
                        or provenance.completeness.value != "complete"
                        or provenance.source_timestamp_verified is not True
                        or stamp is None or stamp > cutoff
                        or received_at > cutoff
                        or (publication_at is not None and publication_at > cutoff)
                        or (revision_at is not None and revision_at > cutoff)
                        or not provenance.source_record_id
                    ):
                        continue
                    timezone_name = str(values.get("timezone") or "").strip()
                    session_id = str(values.get("session_id") or "").strip()
                    dst_status = values.get("dst_status")
                    holiday_status = values.get("holiday_status")
                    is_tradeable = values.get("is_tradeable")
                    if (
                        not session_id
                        or not isinstance(dst_status, bool)
                        or not isinstance(holiday_status, bool)
                        or not isinstance(is_tradeable, bool)
                    ):
                        continue
                    try:
                        ZoneInfo(timezone_name)
                    except (ZoneInfoNotFoundError, ValueError):  # SAFE-FALLBACK: unknown provider timezone cannot establish a session
                        continue
                    try:
                        opened = float(values.get("session_open_epoch"))
                        closed = float(values.get("session_close_epoch"))
                    except (TypeError, ValueError, OverflowError):  # SAFE-FALLBACK: malformed session bounds remain unavailable
                        continue
                    if not all(math.isfinite(v) for v in (opened, closed)) or opened >= closed:
                        continue
                    rollover_start = values.get("rollover_start_epoch")
                    rollover_end = values.get("rollover_end_epoch")
                    try:
                        rollover_start = float(rollover_start) if rollover_start is not None else None
                        rollover_end = float(rollover_end) if rollover_end is not None else None
                    except (TypeError, ValueError, OverflowError):
                        rollover_start = rollover_end = None
                    if (
                        rollover_start is not None and not math.isfinite(rollover_start)
                    ) or (
                        rollover_end is not None and not math.isfinite(rollover_end)
                    ) or (
                        (rollover_start is None) != (rollover_end is None)
                    ) or (
                        rollover_start is not None and rollover_start >= rollover_end
                    ):
                        rollover_start = rollover_end = None
                    provider_sessions.append({
                        "provider": provenance.source.provider,
                        "venue": provenance.source.venue,
                        "record_id": provenance.source_record_id,
                        "session_id": session_id,
                        "source_timestamp_epoch": stamp,
                        "timezone": timezone_name,
                        "session_open_epoch": opened,
                        "session_close_epoch": closed,
                        "dst_status": dst_status,
                        "holiday_status": holiday_status,
                        "rollover_start_epoch": rollover_start,
                        "rollover_end_epoch": rollover_end,
                        "rollover_window": values.get("rollover_window"),
                        "liquidity": values.get("liquidity"),
                        "is_tradeable": is_tradeable,
                    })
            except Exception as exc:  # noqa: BLE001 — unavailable provider calendar remains unknown
                logging.getLogger("apex.cognition.session").warning(
                    "provider session calendar unavailable: %s", type(exc).__name__,
                )
        provider_session = max(provider_sessions, key=lambda row: row["source_timestamp_epoch"]) if provider_sessions else None
        for timeframe, raw in bars_by_tf.items():
            bars = clean_bars(raw)
            if not bars:
                continue
            current = bars[-1]
            session = session_info(current)
            epoch = last_source_epoch(bars)
            if (
                provider_session is not None
                and cutoff is not None
                and epoch is not None
                and epoch <= cutoff
            ):
                # The normalized provider record is the authoritative session
                # observation. Do not merely expose it as side metadata while
                # continuing to calculate state from an unrelated bar payload.
                session = {
                    "verified": True,
                    "session_id": provider_session["session_id"],
                    "open_epoch": provider_session["session_open_epoch"],
                    "close_epoch": provider_session["session_close_epoch"],
                    "timezone": provider_session["timezone"],
                    "dst_status": provider_session["dst_status"],
                    "holiday_status": provider_session["holiday_status"],
                    "rollover_window": provider_session["rollover_window"],
                    "rollover_start_epoch": provider_session["rollover_start_epoch"],
                    "rollover_end_epoch": provider_session["rollover_end_epoch"],
                    "liquidity": provider_session["liquidity"],
                    "is_tradeable": provider_session["is_tradeable"],
                    "minutes_to_next_session": None,
                }
            unknowns = []
            if not session["verified"]:
                unknowns.extend((
                    "authoritative venue session calendar and instrument-specific session timezone",
                    "verified session open/close timestamps with DST and holiday status",
                ))
            if session["dst_status"] is None:
                unknowns.append("provider-verified DST status")
            if session["holiday_status"] is None:
                unknowns.append("provider-verified holiday or special-session status")
            if session["rollover_start_epoch"] is None or session["rollover_end_epoch"] is None:
                unknowns.append("provider-verified rollover window boundaries")
            if session["liquidity"] is None:
                unknowns.append("session-specific liquidity classification or a measured instrument-relative liquidity series")

            state = "unverified_or_missing"
            minutes_to_open = minutes_to_close = None
            rollover_active = None
            if session["verified"] and epoch is not None:
                if session["is_tradeable"] is False or session["holiday_status"] is True:
                    state = "session_closed"
                elif epoch < session["open_epoch"]:
                    state = "pre_session"
                    minutes_to_open = (session["open_epoch"] - epoch) / 60.0
                elif epoch < session["close_epoch"]:
                    state = "in_session"
                    minutes_to_close = (session["close_epoch"] - epoch) / 60.0
                else:
                    state = "session_closed"
                start, end = session["rollover_start_epoch"], session["rollover_end_epoch"]
                if start is not None and end is not None and start < end:
                    rollover_active = start <= epoch <= end

            facts = {
                "status": state, "session_id": session["session_id"],
                "session_open_epoch": session["open_epoch"],
                "session_close_epoch": session["close_epoch"],
                "session_timezone": session["timezone"], "dst_status": session["dst_status"],
                "holiday_status": session["holiday_status"],
                "minutes_to_open": minutes_to_open, "minutes_to_close": minutes_to_close,
                "minutes_to_next_session": session["minutes_to_next_session"],
                "rollover_window": session["rollover_window"], "rollover_active": rollover_active,
                "liquidity": session["liquidity"], "is_tradeable": session["is_tradeable"],
                "calendar_verified": session["verified"],
                "broker_weekly_sessions": broker_sessions if broker_schedule_verified else None,
                "broker_weekly_schedule_available": broker_schedule_verified,
                "broker_session_timezone_verified": spec.get("broker_session_timezone_verified") is True,
                "broker_holiday_calendar_verified": spec.get("broker_holiday_calendar_verified") is True,
                "broker_session_schedule_source": spec.get("source") if broker_schedule_verified else None,
                "provider_session_calendar": provider_session,
                "provider_session_calendar_verified": provider_session is not None,
                "provider_session_calendar_errors": dict(provider_calendar_errors),
            }
            if provider_session is not None:
                facts.update({
                    "provider_session_timezone": provider_session["timezone"],
                    "provider_session_open_epoch": provider_session["session_open_epoch"],
                    "provider_session_close_epoch": provider_session["session_close_epoch"],
                    "provider_dst_status": provider_session["dst_status"],
                    "provider_holiday_status": provider_session["holiday_status"],
                    "provider_rollover_start_epoch": provider_session["rollover_start_epoch"],
                    "provider_rollover_end_epoch": provider_session["rollover_end_epoch"],
                    "provider_liquidity": provider_session["liquidity"],
                    "provider_is_tradeable": provider_session["is_tradeable"],
                })
                if provider_session["dst_status"] is None:
                    unknowns.append("provider session DST status")
                if provider_session["holiday_status"] is None:
                    unknowns.append("provider holiday/special-session status")
            if broker_schedule_verified and provider_session is None:
                unknowns.extend((
                    "broker-server timezone mapping and DST transition rules for weekly session schedule",
                    "authoritative holiday and special-session calendar",
                ))
            readings.append(ConceptReading(
                concept=self.key, timeframe=timeframe, domain=EvidenceDomain.SESSION,
                applicable=True,
                applicability_reason="Session timing and venue state are relevant context for intraday analysis.",
                observation=f"{timeframe} session context is {state}; calendar_verified={session['verified']}. This is contextual evidence, not a trade veto.",
                implications=(Implication(
                    direction=FLAT,
                    statement="Use verified session, DST, holiday and rollover facts as context only; they do not veto a market thesis.",
                    strength=None, activated=False,
                    activation_reason="Session calendar is descriptive context, not a directional trigger.",
                    requires=tuple(unknowns),
                    conflicts_with=("Unverified or static session schedules must not be treated as authoritative.",),
                    management={"meaning": "Reassess time-sensitive theses at verified session transitions; never use this reading as an execution veto."},
                ),),
                facts=facts,source_bar_epoch=epoch,
 source_timestamp_verified=epoch is not None, reference_close=reference,
                reference_timeframe=reference_tf, unknowns=tuple(dict.fromkeys(unknowns)),
            ))
        return readings

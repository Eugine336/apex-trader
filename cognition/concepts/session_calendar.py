"""Verified venue session-calendar reading; never substitutes static UTC windows."""
from __future__ import annotations

from cognition.concepts.auction_utils import clean_bars, last_source_epoch, session_info
from cognition.concepts.base import FLAT, ConceptReading, Implication, reference_price
from cognition.contracts import EvidenceDomain


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
        for timeframe, raw in bars_by_tf.items():
            bars = clean_bars(raw)
            if not bars:
                continue
            current = bars[-1]
            session = session_info(current)
            epoch = last_source_epoch(bars)
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
                if epoch < session["open_epoch"]:
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
            }
            if broker_schedule_verified:
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

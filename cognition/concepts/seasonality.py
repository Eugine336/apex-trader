"""Instrument-own-history calendar tendencies; descriptive evidence, never a trigger."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from statistics import mean, stdev
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cognition.concepts.auction_utils import clean_bars, last_source_epoch, percentile_strength, session_info
from cognition.concepts.base import LONG, SHORT, ConceptReading, Implication, reference_price
from cognition.contracts import EvidenceDomain


# Standard two-sided 95% normal interval definition; labels uncertainty only.
NORMAL_95_CRITICAL_VALUE = 1.959963984540054


def _calendar_key(epoch: float, zone: timezone | ZoneInfo) -> dict:
    local = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(zone)
    return {"weekday": local.weekday(), "hour": local.hour, "month": local.month}


def _effect(records: list[dict], key: str, value: int) -> dict:
    members = [row for row in records if row[key] == value]
    sample = [row["return"] for row in members]
    change = mean(row["price_change"] for row in members) if members else None
    if not sample:
        return {"sample_count": 0, "mean_log_return": None, "mean_price_change": change, "standard_error": None,
                "confidence_interval_95": None, "evidence_label": "insufficient"}
    average = mean(sample)
    if len(sample) < 2:
        return {"sample_count": len(sample), "mean_log_return": average, "mean_price_change": change,
                "standard_error": None, "confidence_interval_95": None,
                "evidence_label": "insufficient"}
    error = stdev(sample) / math.sqrt(len(sample))
    interval = (average - NORMAL_95_CRITICAL_VALUE * error,
                average + NORMAL_95_CRITICAL_VALUE * error)
    label = "strong" if interval[0] > 0 or interval[1] < 0 else "weak"
    return {"sample_count": len(sample), "mean_log_return": average, "mean_price_change": change,
            "standard_error": error, "confidence_interval_95": interval,
            "evidence_label": label}


class SeasonalityConcept:
    key = "seasonality"
    family = "historical_analogue"
    covers_methods = ("seasonality",)
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        readings = []
        for timeframe, raw in bars_by_tf.items():
            bars = clean_bars(raw)
            if len(bars) < 2:
                continue
            current = bars[-1]
            epoch = last_source_epoch(bars)
            unknowns = []
            session = session_info(current)
            zone: timezone | ZoneInfo = timezone.utc
            if session["verified"] and session["timezone"]:
                try:
                    zone = ZoneInfo(session["timezone"])
                except (ZoneInfoNotFoundError, ValueError):
                    unknowns.append("valid instrument-local timezone for DST-aware calendar buckets")
            else:
                unknowns.append("verified instrument-local timezone; reported calendar buckets use UTC")
            records = []
            if epoch is None:
                unknowns.append("source-verified timestamps for completed bars")
            else:
                for previous, bar in zip(bars, bars[1:]):
                    previous_epoch, bar_epoch = previous.get("timestamp_epoch"), bar.get("timestamp_epoch")
                    if previous.get("source_timestamp_verified") is not True or bar.get("source_timestamp_verified") is not True:
                        continue
                    if not isinstance(previous_epoch, (int, float)) or not isinstance(bar_epoch, (int, float)):
                        continue
                    if previous["close"] <= 0 or bar["close"] <= 0:
                        continue
                    records.append({
                        **_calendar_key(float(bar_epoch), zone),
                        "return": math.log(bar["close"] / previous["close"]),
                        "price_change": bar["close"] - previous["close"],
                        "epoch": float(bar_epoch),
                    })
            current_key = _calendar_key(epoch, zone) if epoch is not None else None
            prior_records = records[:-1] if records else []
            effects = {}
            if current_key is not None:
                effects = {
                    "day_of_week": _effect(prior_records, "weekday", current_key["weekday"]),
                    "time_of_day": _effect(prior_records, "hour", current_key["hour"]),
                    "month": _effect(prior_records, "month", current_key["month"]),
                }
            else:
                unknowns.append("calendar buckets cannot be assigned without a verified source timestamp")
            available = [item for item in effects.values() if item["mean_price_change"] is not None]
            # Log-return magnitudes depend on the price origin. Retain them for
            # reporting and rank the observed signed changes in quote units.
            historical_magnitudes = []
            for key in ("weekday", "hour", "month"):
                values = {record[key] for record in prior_records}
                historical_magnitudes.extend(
                    abs(_effect(prior_records, key, value)["mean_price_change"]) for value in values
                )
            changes = [item["mean_price_change"] for item in available]
            long_effect = max((value for value in changes if value > 0), default=None)
            short_effect = min((value for value in changes if value < 0), default=None)
            log_means = [item["mean_log_return"] for item in available]
            long_strength = percentile_strength(abs(long_effect), historical_magnitudes) if long_effect is not None else None
            short_strength = percentile_strength(abs(short_effect), historical_magnitudes) if short_effect is not None else None
            if not available:
                unknowns.append("historical occurrences of the current weekday, hour and month buckets")
            unknowns.append("a price objective and invalidation must come from structural or auction concepts")
            description = (
                f"Prior completed returns: weekday={effects.get('day_of_week')}, "
                f"hour={effects.get('time_of_day')}, month={effects.get('month')}."
            )
            implications = (
                Implication(
                    direction=LONG,
                    statement=f"Historical calendar tendency for the current bucket set is {'positive' if long_effect is not None else 'not established'}. {description}",
                    strength=long_strength, activated=False,
                    activation_reason="Seasonality is context only and never an entry trigger.",
                    invalidation_reason="The calendar bucket ended or subsequent realised returns no longer resemble its historical distribution.",
                    requires=tuple(dict.fromkeys(unknowns)),
                    conflicts_with=("Current completed price action contradicts the historical calendar tendency.",),
                    management={"meaning": "Use sample count and uncertainty interval as context; require independent price-structure evidence."},
                ),
                Implication(
                    direction=SHORT,
                    statement=f"Historical calendar tendency for the current bucket set is {'negative' if short_effect is not None else 'not established'}. {description}",
                    strength=short_strength, activated=False,
                    activation_reason="Seasonality is context only and never an entry trigger.",
                    invalidation_reason="The calendar bucket ended or subsequent realised returns no longer resemble its historical distribution.",
                    requires=tuple(dict.fromkeys(unknowns)),
                    conflicts_with=("Current completed price action contradicts the historical calendar tendency.",),
                    management={"meaning": "Use sample count and uncertainty interval as context; require independent price-structure evidence."},
                ),
            )
            readings.append(ConceptReading(
                concept=self.key, timeframe=timeframe, domain=EvidenceDomain.HISTORICAL_ANALOGUE,
                applicable=bool(records),
                applicability_reason="Calendar tendencies require source-verified completed-bar timestamps and repeated historical occurrences.",
                observation=f"{timeframe} calendar tendencies are descriptive and are not predictive rules.",
                implications=implications,
                facts={
                    "calendar_timezone": str(getattr(zone, "key", "UTC")),
                    "calendar_bucket": current_key, "effects": effects,
                    "prior_return_samples": len(prior_records),
                    "long_effect_log_return": max((v for v in log_means if v > 0), default=None),
                    "short_effect_log_return": min((v for v in log_means if v < 0), default=None),
                    "long_effect_price_change": long_effect, "short_effect_price_change": short_effect,
                    "strength_basis": "absolute_mean_close_change_in_quote_units",
                    "strength_is_percentile_of_own_history": True,
                    "normal_95_critical_value_is_uncertainty_definition": NORMAL_95_CRITICAL_VALUE,
                },source_bar_epoch=epoch,

                source_timestamp_verified=epoch is not None, reference_close=reference, reference_timeframe=reference_tf,
                unknowns=tuple(dict.fromkeys(unknowns)),
            ))
        return readings

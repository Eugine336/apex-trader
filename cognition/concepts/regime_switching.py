"""Descriptive distribution-shift diagnostics for completed-bar returns.

This is not a predictive regime classifier. It exposes possible mean/variance
changes for the Brain to challenge against structure, volatility and execution
context. Thresholds are diagnostic conventions, not calibrated probabilities.
"""
from __future__ import annotations

import math
from statistics import mean, variance

from cognition.concepts.base import FLAT, TIMEFRAME_SECONDS, ConceptReading, Implication, finite, reference_price
from cognition.contracts import EvidenceDomain


class RegimeSwitchingConcepts:
    key = "regime_switching"
    family = "quantitative"
    covers_methods = ("regime_switching",)
    covers_lenses = ("market_regime_transition",)

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        close, close_tf = reference_price(bars_by_tf)
        readings = []
        for timeframe, raw in bars_by_tf.items():
            if timeframe not in TIMEFRAME_SECONDS or TIMEFRAME_SECONDS[timeframe] < 300:
                continue
            bars = []
            for bar in raw or []:
                values = {key: finite(bar.get(key)) for key in ("open", "high", "low", "close")}
                if any(value is None for value in values.values()):
                    continue
                if values["high"] < max(values["open"], values["close"]):
                    continue
                if values["low"] > min(values["open"], values["close"]):
                    continue
                if values["high"] < values["low"]:
                    continue
                bars.append({**bar, **values})
            if len(bars) < 61:
                continue
            closes = [float(bar["close"]) for bar in bars]
            returns = [
                math.log(current / previous)
                for previous, current in zip(closes, closes[1:])
                if previous > 0 and current > 0
            ]
            if len(returns) < 60:
                continue
            baseline, recent = returns[-60:-30], returns[-30:]
            base_mean, recent_mean = mean(baseline), mean(recent)
            base_var = variance(baseline)
            recent_var = variance(recent)
            pooled = ((len(baseline)-1)*base_var + (len(recent)-1)*recent_var) / (len(baseline)+len(recent)-2)
            standard_error = math.sqrt(pooled * (1/len(baseline) + 1/len(recent))) if pooled > 0 else 0.0
            mean_shift_t = (recent_mean-base_mean)/standard_error if standard_error > 0 else 0.0
            variance_ratio = recent_var/base_var if base_var > 0 else None
            def lag_one(values):
                centre = mean(values)
                left = [x-centre for x in values[:-1]]
                right = [x-centre for x in values[1:]]
                denom = math.sqrt(sum(x*x for x in left)*sum(x*x for x in right))
                return sum(a*b for a,b in zip(left,right))/denom if denom > 0 else None
            base_autocorr, recent_autocorr = lag_one(baseline), lag_one(recent)
            mean_shift = abs(mean_shift_t) >= 2.0
            variance_shift = variance_ratio is not None and (
                variance_ratio >= 2.0 or variance_ratio <= 0.5
            )
            detected = mean_shift or variance_shift
            direction = "upward_mean_shift" if mean_shift_t > 0 else (
                "downward_mean_shift" if mean_shift_t < 0 else "none"
            )
            epoch = finite(bars[-1].get("timestamp_epoch")) if bars[-1].get("source_timestamp_verified") is True else None
            facts = {
                "baseline_return_count": len(baseline), "recent_return_count": len(recent),
                "baseline_mean_log_return": base_mean, "recent_mean_log_return": recent_mean,
                "mean_shift_t_statistic": mean_shift_t, "mean_shift_detected": mean_shift,
                "baseline_return_variance": base_var, "recent_return_variance": recent_var,
                "variance_ratio_recent_to_baseline": variance_ratio,
                "variance_shift_detected": variance_shift, "possible_transition": detected,
                "mean_shift_direction": direction,
                "baseline_lag1_autocorrelation": base_autocorr,
                "recent_lag1_autocorrelation": recent_autocorr,
                "windows": {"baseline": "previous 30 log returns", "recent": "latest 30 log returns"},
                "diagnostic_conventions": {"absolute_mean_shift_t": 2.0, "variance_ratio_upper": 2.0,
                                           "variance_ratio_lower": 0.5},
                "definition": "two_nonoverlapping_30_return_welch_mean_and_variance_shift_v1",
            }
            readings.append(ConceptReading(
                concept="regime_switching", timeframe=timeframe, domain=EvidenceDomain.VOLATILITY,
                applicable=True,
                applicability_reason="Two complete 30-return samples are available for a descriptive distribution comparison",
                observation=(f"{timeframe} distribution comparison: mean-shift t={mean_shift_t:.3f}; "
                             f"variance ratio={variance_ratio}; possible transition={detected}."),
                implications=(Implication(
                    direction=FLAT,
                    statement="Possible statistical regime transition; reassess assumptions and reduce confidence in stale calibration",
                    strength=None, activated=detected,
                    activation_reason="Recent return distribution differs from the preceding window under declared diagnostics"
                                    if detected else "No declared mean/variance shift diagnostic crossed its descriptive threshold",
                    invalidation_reason="A longer stable sample and independent market-context review may resolve the apparent shift",
                    requires=("cross-timeframe confirmation", "compare structure and volatility evidence",
                              "revalidate strategy calibration out of sample"),
                    conflicts_with=("short samples and autocorrelation violate simple independent-return assumptions",)
                                    if abs((recent_autocorr or 0.0)) > 0.3 else (),
                    management={"role": "regime_warning_only", "reassess_on": "next completed bar",
                                "never_authorizes_entry": True},
                ),),
                facts=facts,source_bar_epoch=epoch,
 source_timestamp_verified=epoch is not None, reference_close=close,
                reference_timeframe=close_tf,
                unknowns=("The two-window test is sensitive to autocorrelation, overlapping regime changes and window choice; it is not a calibrated change-point probability.",),
            ))
        return readings

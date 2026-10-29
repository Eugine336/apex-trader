"""Point-in-time validation guards for historical market evidence.

These checks detect common temporal leakage and split contamination. Passing
them is necessary but not sufficient for predictive validity; statistical
power, data provenance, market realism and independent review still matter.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


def _time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return parsed.astimezone(timezone.utc).timestamp()
        except (ValueError, OverflowError):
            return None
    return None


def validate_historical_rows(
    rows: Iterable[dict[str, Any]],
    *,
    untouched_holdout: bool = False,
    multiple_testing_mitigated: bool = False,
) -> dict[str, Any]:
    """Validate timestamp ordering and split isolation for point-in-time rows.

    Each row requires sample_id, split, decision_timestamp, feature_timestamps
    and outcome_timestamp. Timestamps must be epoch numbers or timezone-aware
    ISO-8601 strings. Missing timestamps fail validation rather than being filled.
    """
    items = list(rows)
    errors: list[str] = []
    parsed: list[dict[str, Any]] = []
    seen: dict[str, set[str]] = {"train": set(), "validation": set(), "test": set()}
    sample_split_by_id: dict[str, str] = {}
    for index, row in enumerate(items):
        if not isinstance(row, dict):
            errors.append(f"row {index}: row must be a mapping")
            continue
        sample_id = str(row.get("sample_id", "") or "")
        split = str(row.get("split", "") or "").strip().lower()
        decision = _time(row.get("decision_timestamp"))
        outcome = _time(row.get("outcome_timestamp"))
        features = row.get("feature_timestamps")
        if not sample_id:
            errors.append(f"row {index}: missing sample_id")
        if split not in seen:
            errors.append(f"row {index}: split must be train, validation or test")
        if decision is None or outcome is None:
            errors.append(f"row {index}: missing/invalid timezone-aware decision or outcome timestamp")
        if not isinstance(features, list) or not features:
            errors.append(f"row {index}: feature_timestamps missing or empty")
            feature_times = []
        else:
            feature_times = [_time(value) for value in features]
            if any(value is None for value in feature_times):
                errors.append(f"row {index}: missing/invalid feature timestamp")
        if decision is not None and any(value is not None and value > decision for value in feature_times):
            errors.append(f"row {index}: future feature timestamp leaks beyond decision time")
        if decision is not None and outcome is not None and outcome <= decision:
            errors.append(f"row {index}: outcome timestamp is not after decision time")
        if sample_id and split in seen:
            if sample_id in seen[split]:
                errors.append(f"row {index}: duplicate sample_id within {split} split")
            prior_split = sample_split_by_id.get(sample_id)
            if prior_split is not None and prior_split != split:
                errors.append(
                    f"row {index}: sample_id appears in both {prior_split} and {split} splits"
                )
            sample_split_by_id[sample_id] = split
            seen[split].add(sample_id)
        parsed.append({
            "sample_id": sample_id, "split": split,
            "decision": decision, "outcome": outcome,
        })

    for earlier, later in (("train", "validation"), ("train", "test"), ("validation", "test")):
        overlap = seen[earlier] & seen[later]
        if overlap:
            errors.append(f"sample IDs overlap between {earlier} and {later} splits")

    # A training outcome may extend beyond its decision timestamp. Require the
    # entire earlier outcome horizon to end before the next split begins, so a
    # long-horizon label cannot bleed into validation/test time.
    for earlier, later in (("train", "validation"), ("validation", "test")):
        earlier_outcomes = [
            r["outcome"] for r in parsed
            if r["split"] == earlier and r["outcome"] is not None
        ]
        later_decisions = [
            r["decision"] for r in parsed
            if r["split"] == later and r["decision"] is not None
        ]
        if earlier_outcomes and later_decisions and max(earlier_outcomes) >= min(later_decisions):
            errors.append(
                f"{earlier}/{later} split outcome horizon overlaps later decision window"
            )

    if not untouched_holdout:
        errors.append("untouched holdout is not attested")
    if not multiple_testing_mitigated:
        errors.append("multiple-testing/selection-bias mitigation is not attested")
    counts = {split: sum(row["split"] == split for row in parsed) for split in seen}
    passed = not errors and all(counts[split] > 0 for split in seen)
    if not all(counts[split] > 0 for split in seen):
        errors.append("train, validation and test splits must all contain rows")
        passed = False
    return {
        "passed": passed,
        "rows_checked": len(items),
        "split_counts": counts,
        "errors": errors,
        "predictive_ability_demonstrated": False,
        "scope_note": "Temporal checks are necessary, not sufficient; no return or profitability claim is made.",
    }


__all__ = ["validate_historical_rows"]

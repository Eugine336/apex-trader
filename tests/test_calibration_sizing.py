"""Brain calibration is observational and must never scale position size."""

from dataclasses import fields

from cognition.influence import CalibrationTracker
from portfolio.models import SizingFactors


def test_calibration_is_observability_only():
    tracker = CalibrationTracker()
    assert tracker.reliability() == 0.0
    assert not hasattr(tracker, "sizing_scale")
    assert "sizing_scale" not in tracker.get_status()


def test_well_calibrated_outcomes_raise_only_the_reliability_metric():
    tracker = CalibrationTracker(min_samples=30)
    tracker.observe_many([(0.6, i % 5 < 3) for i in range(300)], source="hindcast")
    assert tracker.metrics()["reliability_gap"] < 0.02
    assert tracker.reliability() > 0.85
    assert not hasattr(tracker, "sizing_scale")


def test_overconfident_outcomes_lower_reliability_without_changing_size():
    tracker = CalibrationTracker(min_samples=30)
    tracker.observe_many([(0.9, i % 5 < 2) for i in range(300)], source="live")
    assert tracker.reliability() == 0.0
    assert not hasattr(tracker, "sizing_scale")


def test_calibration_persists_across_restart_and_tracks_sources(tmp_path):
    path = tmp_path / "brain_calibration.json"
    first = CalibrationTracker(path=path)
    first.observe_many([(0.7, True)] * 40, source="hindcast")
    first.observe(0.7, False)
    second = CalibrationTracker(path=path)
    assert second.metrics()["samples"] == 41
    assert second.metrics()["sources"] == {"hindcast": 40, "live": 1}
    assert second.reliability() == first.reliability()


def test_corrupt_calibration_file_means_unverified_not_a_crash(tmp_path):
    path = tmp_path / "brain_calibration.json"
    path.write_text("{not json", encoding="utf-8")
    tracker = CalibrationTracker(path=path)
    assert tracker.metrics()["samples"] == 0
    assert tracker.reliability() == 0.0


def test_malformed_pairs_are_skipped_not_fatal():
    tracker = CalibrationTracker()
    assert tracker.observe_many([(0.6, True), object(), (0.5,), (0.4, False)]) == 2


def test_position_sizing_has_no_confidence_or_calibration_multiplier():
    names = {item.name for item in fields(SizingFactors)}
    assert "de_size_mult" not in names
    assert "calib_mult" not in names
    factor_names = {name for name, _value in SizingFactors().as_ordered()}
    assert "DE" not in factor_names
    assert "CALIB" not in factor_names

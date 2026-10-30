"""Verified calibration scales risk (never to zero) and survives restarts."""

import types

import event_driven_bootstrap as edb
from cognition.influence import CalibrationTracker
from portfolio.models import SizingFactors


def test_unverified_calibration_trades_at_the_floor_not_zero():
    t = CalibrationTracker()
    assert t.reliability() == 0.0
    assert t.sizing_scale() == CalibrationTracker.SIZE_FLOOR > 0


def test_well_calibrated_outcomes_raise_the_scale_towards_one():
    t = CalibrationTracker(min_samples=30)
    # stated 0.6, wins 60%: well calibrated
    t.observe_many([(0.6, i % 5 < 3) for i in range(300)], source="hindcast")
    assert t.metrics()["reliability_gap"] < 0.02
    assert t.sizing_scale() > 0.85


def test_overconfident_calibration_does_not_earn_size():
    t = CalibrationTracker(min_samples=30)
    # stated 0.9, wins 40%: verified miscalibration
    t.observe_many([(0.9, i % 5 < 2) for i in range(300)], source="live")
    assert t.sizing_scale() == CalibrationTracker.SIZE_FLOOR


def test_scale_is_monotonic_in_evidence_volume():
    scales = []
    for n in (0, 10, 30, 120, 480):
        t = CalibrationTracker(min_samples=30, history_limit=1000)
        t.observe_many([(0.6, i % 5 < 3) for i in range(n)])
        scales.append(t.sizing_scale())
    assert scales == sorted(scales) and scales[0] < scales[-1] <= 1.0


def test_calibration_persists_across_restart_and_tracks_sources(tmp_path):
    path = tmp_path / "brain_calibration.json"
    a = CalibrationTracker(path=path)
    a.observe_many([(0.7, True)] * 40, source="hindcast")
    a.observe(0.7, False)  # live
    b = CalibrationTracker(path=path)
    assert b.metrics()["samples"] == 41
    assert b.metrics()["sources"] == {"hindcast": 40, "live": 1}
    assert b.sizing_scale() == a.sizing_scale()


def test_corrupt_calibration_file_means_unverified_not_a_crash(tmp_path):
    path = tmp_path / "brain_calibration.json"
    path.write_text("{not json", encoding="utf-8")
    t = CalibrationTracker(path=path)
    assert t.metrics()["samples"] == 0
    assert t.sizing_scale() == CalibrationTracker.SIZE_FLOOR


def test_malformed_pairs_are_skipped_not_fatal():
    t = CalibrationTracker()
    assert t.observe_many([(0.6, True), object(), (0.5,), (0.4, False)]) == 2


def test_calibration_factor_enters_the_sizing_product_and_only_de_risks():
    base = SizingFactors(base_risk_pct=0.01)
    scaled = SizingFactors(base_risk_pct=0.01, calib_mult=0.25)
    assert ("CALIB", 0.25) in scaled.as_ordered()
    assert scaled.product() == base.product() * 0.25


def test_bootstrap_scale_uses_tracker_and_falls_back_to_floor():
    def _scale(ctx):
        ns = types.SimpleNamespace(_ctx=ctx)
        return types.MethodType(edb.EventDrivenSystem._calibration_size_scale, ns)()

    t = CalibrationTracker()
    t.observe_many([(0.6, i % 5 < 3) for i in range(300)])
    assert _scale(types.SimpleNamespace(brain_calibration=t)) == t.sizing_scale()
    assert _scale(types.SimpleNamespace(brain_calibration=None)) == 0.25
    assert _scale(None) == 0.25

    class _Broken:
        def sizing_scale(self):
            raise RuntimeError("x")

    assert _scale(types.SimpleNamespace(brain_calibration=_Broken())) == 0.25

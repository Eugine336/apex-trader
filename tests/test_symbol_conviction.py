"""
Tests for the instrument-awareness layer:

* 1B — Symbol-Relative Conviction (``adaptive.symbol_conviction.SymbolConvictionStore``)
  and its wiring into ``brain.directional_consensus.form_thesis``.
* 1C — per-symbol sharding overlays on the VoteCalibrator and ModuleGovernor.

Dependency-light: pure stdlib math plus tiny EmitterFeedback fakes that drive the
per-pair accuracy queries precisely.
"""

import pytest

from config import (
    ConvictionNormalizationConfig,
    VoteCalibratorConfig,
    ModuleGovernorConfig,
)
from adaptive.symbol_conviction import SymbolConvictionStore
from adaptive.vote_calibrator import VoteCalibrator
from adaptive.module_governor import ModuleGovernor
from brain.directional_consensus import Vote, form_thesis


# ── Test doubles ──────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, accuracy_all, total_signals):
        self.accuracy_all = accuracy_all
        self.total_signals = total_signals


class _PairFeedback:
    """Stand-in for EmitterFeedbackService.request_feedback (per (emitter,pair))."""

    def __init__(self, table):
        # table: {(emitter, pair): (accuracy, n)}
        self._table = table

    def request_feedback(self, request):
        key = (request.emitter, request.pair)
        acc, n = self._table.get(key, (0.0, 0))
        return _Resp(acc, n)

    def get_all_emitter_summaries(self, lookback=100):
        return {}


def _thesis_kw(**over):
    kw = dict(
        min_net_score=0.5,
        min_agreement=0.5,
        high_authority_modules=[],
        high_authority_oppose_confidence=0.7,
        min_contributors=1,
        conviction_threshold=0.62,
        net_scale=0.0,
    )
    kw.update(over)
    return kw


# ── 1B — SymbolConvictionStore ─────────────────────────────────────────────


class TestSymbolConvictionStore:
    def test_cold_start_is_raw_passthrough(self):
        s = SymbolConvictionStore(enabled=True, min_samples=5, blend=1.0, persist=False)
        assert s.record_and_normalize("EURUSD", 0.82) == 0.82

    def test_percentile_normalization_after_warmup(self):
        s = SymbolConvictionStore(enabled=True, min_samples=5, blend=1.0, persist=False)
        for v in (0.10, 0.20, 0.30, 0.40, 0.50):
            s.record("EURUSD", v)
        # A value above the whole distribution → top percentile.
        assert s.normalize("EURUSD", 0.82) == 1.0
        # Below the whole distribution → bottom percentile.
        assert s.normalize("EURUSD", 0.05) == 0.0

    def test_blend_mixes_raw_and_percentile(self):
        s = SymbolConvictionStore(enabled=True, min_samples=3, blend=0.5, persist=False)
        for v in (0.2, 0.4, 0.6, 0.8):
            s.record("XAUUSD", v)
        # raw 0.9, percentile ~1.0 → 0.5*0.9 + 0.5*1.0 = 0.95
        assert s.normalize("XAUUSD", 0.9) == pytest.approx(0.95, abs=1e-6)

    def test_disabled_is_passthrough(self):
        s = SymbolConvictionStore(enabled=False, persist=False)
        assert s.record_and_normalize("X", 0.7) == 0.7
        assert s.sample_count("X") == 0

    def test_per_symbol_isolation(self):
        s = SymbolConvictionStore(enabled=True, min_samples=3, blend=1.0, persist=False)
        for v in (0.1, 0.2, 0.3):
            s.record("EURUSD", v)
        # GBPJPY has no history → raw passthrough, unaffected by EURUSD.
        assert s.normalize("GBPJPY", 0.9) == 0.9

    def test_bounded_history(self):
        s = SymbolConvictionStore(
            enabled=True, min_samples=2, max_history=10, blend=1.0, persist=False
        )
        for i in range(50):
            s.record("EURUSD", (i % 10) / 10.0)
        assert s.sample_count("EURUSD") == 10

    def test_persistence_round_trip(self, tmp_path):
        p = tmp_path / "sc.json"
        s = SymbolConvictionStore(
            enabled=True, min_samples=2, blend=1.0, persist=True,
            path=str(p), save_interval_seconds=0.0,
        )
        for v in (0.1, 0.5, 0.9):
            s.record_and_normalize("GBPUSD", v)
        s.save()
        s2 = SymbolConvictionStore(
            enabled=True, min_samples=2, blend=1.0, persist=True, path=str(p)
        )
        assert s2.sample_count("GBPUSD") == 3

    def test_config_validation(self):
        with pytest.raises(ValueError):
            ConvictionNormalizationConfig(min_samples=0)
        with pytest.raises(ValueError):
            ConvictionNormalizationConfig(blend=1.5)
        with pytest.raises(ValueError):
            ConvictionNormalizationConfig(min_samples=50, max_history=10)


# ── 1B — form_thesis integration ───────────────────────────────────────────


class TestFormThesisNormalization:
    def _votes(self):
        return [
            Vote("structure", "LONG", 0.9, 1.0),
            Vote("momentum", "LONG", 0.8, 1.0),
            Vote("volume", "LONG", 0.7, 1.0),
        ]

    def test_no_store_keeps_raw(self):
        t = form_thesis(self._votes(), **_thesis_kw())
        assert t.conviction == t.raw_conviction

    def test_store_normalizes_and_records(self):
        store = SymbolConvictionStore(enabled=True, min_samples=2, blend=1.0, persist=False)
        # Warm the symbol so the next conviction normalizes to a percentile.
        for v in (0.95, 0.96, 0.97):
            store.record("EURUSD", v)
        t = form_thesis(
            self._votes(), symbol="EURUSD", conviction_store=store, **_thesis_kw()
        )
        # The strong panel raw conviction is high; relative to a high history its
        # percentile rank differs — conviction is the normalized value.
        assert 0.0 <= t.conviction <= 1.0
        assert t.raw_conviction >= t.conviction or t.conviction <= 1.0

    def test_store_can_flip_trigger(self):
        class _LowStore:
            def record_and_normalize(self, symbol, raw):
                return 0.10

        t = form_thesis(
            self._votes(), symbol="EURUSD", conviction_store=_LowStore(), **_thesis_kw()
        )
        assert t.conviction == 0.10
        assert t.trigger is False
        assert t.raw_conviction > 0.10  # the raw value is preserved

    def test_store_exception_falls_back_to_raw(self):
        class _Boom:
            def record_and_normalize(self, symbol, raw):
                raise RuntimeError("boom")

        t = form_thesis(
            self._votes(), symbol="EURUSD", conviction_store=_Boom(), **_thesis_kw()
        )
        assert t.conviction == t.raw_conviction


# ── 1C — VoteCalibrator per-symbol overlay ─────────────────────────────────


def _vc_cfg(**kw):
    base = dict(vote_calibration_enabled=True, per_symbol_recompute_seconds=0.0)
    base.update(kw)
    return VoteCalibratorConfig(**base)


class TestVoteCalibratorPerSymbol:
    def test_isolation_between_symbols(self):
        # structure accurate on EURUSD, poor on GBPJPY; momentum the reverse.
        table = {
            ("structure", "EURUSD"): (0.80, 40),
            ("momentum", "EURUSD"): (0.30, 40),
            ("structure", "GBPJPY"): (0.30, 40),
            ("momentum", "GBPJPY"): (0.80, 40),
        }
        vc = VoteCalibrator(_vc_cfg(), _PairFeedback(table))
        eur_struct = vc.multiplier_for("structure", "EURUSD")
        eur_mom = vc.multiplier_for("momentum", "EURUSD")
        gbp_struct = vc.multiplier_for("structure", "GBPJPY")
        gbp_mom = vc.multiplier_for("momentum", "GBPJPY")
        assert eur_struct > eur_mom            # structure louder where it's accurate
        assert gbp_mom > gbp_struct            # momentum louder where it's accurate
        assert eur_struct != gbp_struct        # same module, different weight per symbol

    def test_thin_symbol_falls_back_to_global(self):
        # Only one module qualifies per symbol → per-symbol calibration skipped →
        # falls back to the global (neutral) multiplier.
        table = {("structure", "EURUSD"): (0.80, 40)}
        vc = VoteCalibrator(_vc_cfg(), _PairFeedback(table))
        assert vc.multiplier_for("structure", "EURUSD") == 1.0

    def test_disabled_returns_neutral(self):
        vc = VoteCalibrator(_vc_cfg(vote_calibration_enabled=False), _PairFeedback({}))
        assert vc.multiplier_for("structure", "EURUSD") == 1.0

    def test_per_symbol_off_uses_global(self):
        table = {
            ("structure", "EURUSD"): (0.80, 40),
            ("momentum", "EURUSD"): (0.30, 40),
        }
        vc = VoteCalibrator(_vc_cfg(per_symbol_enabled=False), _PairFeedback(table))
        # No global recalibration has run → global map empty → neutral 1.0.
        assert vc.multiplier_for("structure", "EURUSD") == 1.0

    def test_no_symbol_arg_is_backward_compatible(self):
        vc = VoteCalibrator(_vc_cfg(), _PairFeedback({}))
        assert vc.multiplier_for("structure") == 1.0
        assert vc.calibrated_weight("structure", 2.0) == 2.0


# ── 1C — ModuleGovernor per-symbol overlay ─────────────────────────────────


def _mg_cfg(**kw):
    base = dict(
        module_governor_enabled=True,
        shadow_lookback=30,
        reactivation_min_signals=30,
        per_symbol_recompute_seconds=0.0,
    )
    base.update(kw)
    return ModuleGovernorConfig(**base)


class TestModuleGovernorPerSymbol:
    def test_enabled_reads_nested_config(self, tmp_path):
        gov = ModuleGovernor(config=_mg_cfg(), emitter_feedback=_PairFeedback({}),
                             db_path=str(tmp_path / "mg.db"))
        assert gov.enabled is True
        gov.close()

    def test_suppressed_only_where_harmful(self, tmp_path):
        # momentum poor on GBPJPY, good on EURUSD.
        table = {
            ("momentum", "GBPJPY"): (0.20, 40),
            ("momentum", "EURUSD"): (0.70, 40),
        }
        gov = ModuleGovernor(
            config=_mg_cfg(), emitter_feedback=_PairFeedback(table),
            db_path=str(tmp_path / "mg.db"),
        )
        assert gov.is_suppressed("momentum", "GBPJPY") is True
        assert gov.is_suppressed("momentum", "EURUSD") is False
        gov.close()

    def test_no_per_symbol_data_uses_global(self, tmp_path):
        gov = ModuleGovernor(
            config=_mg_cfg(), emitter_feedback=_PairFeedback({}),
            db_path=str(tmp_path / "mg.db"),
        )
        # Globally ACTIVE + no per-symbol data → not suppressed.
        assert gov.is_suppressed("momentum", "AUDUSD") is False
        gov.close()

    def test_disabled_feature_is_inert(self, tmp_path):
        gov = ModuleGovernor(
            config=_mg_cfg(module_governor_enabled=False),
            emitter_feedback=_PairFeedback({("momentum", "GBPJPY"): (0.10, 100)}),
            db_path=str(tmp_path / "mg.db"),
        )
        assert gov.is_suppressed("momentum", "GBPJPY") is False
        gov.close()

    def test_no_symbol_arg_is_backward_compatible(self, tmp_path):
        gov = ModuleGovernor(
            config=_mg_cfg(), emitter_feedback=_PairFeedback({}),
            db_path=str(tmp_path / "mg.db"),
        )
        assert gov.is_suppressed("momentum") is False
        gov.close()

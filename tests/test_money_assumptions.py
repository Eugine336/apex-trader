"""
Tests for fail-closed money-path guards:
  A. Unknown instrument rejected in RiskEngine.assess()
  B. Unknown instrument rejected in EntryEngine.calculate_entry()
  C. Startup aborts when enabled pairs ⊄ INSTRUMENT_REGISTRY
  D. Non-positive balance rejected in RiskEngine.assess()
  E. Weekly limit reads from config, not a hardcoded 8.0
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

# ── stub heavy deps before any project imports ──────────────────────────
_STUB_MODULES = [
    "MetaTrader5", "requests", "websockets",
    "websockets.sync", "websockets.sync.client",
    "aiosqlite", "sqlalchemy",
    "torch", "torch.nn", "torch.nn.functional",
    "torch.optim", "torch.distributions",
]
for mod in _STUB_MODULES:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

_loguru = sys.modules.setdefault("loguru", MagicMock())
_loguru.logger = MagicMock()

_np = ModuleType("numpy")
_np.__version__ = "1.26.0"
_np.ndarray = list
_np.float64 = float
_np.array = lambda x, **kw: list(x) if hasattr(x, "__iter__") else [x]
_np.zeros = lambda n: [0] * n
_np.mean = lambda x: sum(x) / len(x) if x else 0.0
_np.std = lambda x, **kw: 0.0
_np.nan = float("nan")
_np.isnan = lambda x: x != x
if "numpy" not in sys.modules:
    sys.modules["numpy"] = _np

_pd = ModuleType("pandas")


class _FakeTS:
    @staticmethod
    def now(tz=None):
        return _FakeTS()

    def isoformat(self):
        return "2025-01-01T00:00:00+00:00"


_pd.Timestamp = _FakeTS
_pd.Series = MagicMock
_pd.DataFrame = MagicMock
if "pandas" not in sys.modules:
    sys.modules["pandas"] = _pd

import pytest

from risk.risk_engine import RiskEngine
from trigger.entry_engine import EntryEngine, EntryRejection
from config import RiskConfig, INSTRUMENT_REGISTRY
from platforms.startup_check import StartupCheck, CheckResult


# ── helpers ────────────────────────────────────────────────────────────

def _make_scan_result(pair="EURUSD", direction="LONG", score=85):
    from scanner.pair_scanner import PairScanResult
    return PairScanResult(
        pair=pair, direction=direction, score=score,
        regime="TRENDING",
        trend_h4="BULLISH" if direction == "LONG" else "BEARISH",
        trend_h1="BULLISH" if direction == "LONG" else "BEARISH",
        bias_strength="STRONG",
        has_fvg=True, has_order_block=True, has_liquidity_target=False,
        sweep_detected=False, inducement_detected=False,
        wyckoff_phase="MARKUP" if direction == "LONG" else "MARKDOWN",
        volume_confirmation=True, session_active=True,
        currency_strength_aligned=True, status="READY",
        timestamp=datetime.now(timezone.utc),
        confluences=["Structure aligned", "FVG entry zone"],
    )


def _make_other_criticals_pass(checker: StartupCheck):
    ok = lambda name: CheckResult(name=name, passed=True, message="ok", duration_ms=0.1)
    checker._check_imports = lambda: ok("imports")
    checker._check_config = lambda: ok("config")
    checker._check_database = lambda: ok("database")
    checker._check_disk_space = lambda: ok("disk_space")


# ═══════════════════════════════════════════════════════════════════════
# A. Unknown instrument → RiskEngine rejects (lots path)
# ═══════════════════════════════════════════════════════════════════════

class TestUnknownInstrumentRiskEngine:

    def test_unknown_pair_rejected(self):
        engine = RiskEngine(starting_balance=10_000.0)
        result = engine.assess(
            pair="FAKEXYZ",
            direction="LONG",
            entry_price=100.0,
            stop_loss=99.0,
            account_balance=10_000.0,
        )
        assert result.approved is False
        assert any("Unknown instrument" in r for r in result.rejections)

    def test_unknown_pair_does_not_use_assumed_economics(self):
        engine = RiskEngine(starting_balance=10_000.0)
        result = engine.assess(
            pair="FAKEXYZ",
            direction="LONG",
            entry_price=100.0,
            stop_loss=99.0,
            account_balance=10_000.0,
        )
        assert result.approved is False
        assert result.position_size_lots == 0.0

    def test_known_pair_passes_instrument_check(self):
        engine = RiskEngine(starting_balance=10_000.0)
        assert "EURUSD" in INSTRUMENT_REGISTRY
        result = engine.assess(
            pair="EURUSD",
            direction="LONG",
            entry_price=1.10000,
            stop_loss=1.09800,
            account_balance=10_000.0,
            current_spread_pips=1.0,
        )
        assert not any("Unknown instrument" in r for r in result.rejections)


# ═══════════════════════════════════════════════════════════════════════
# B. Unknown instrument → EntryEngine rejects
# ═══════════════════════════════════════════════════════════════════════

class TestUnknownInstrumentEntryEngine:

    def test_unknown_pair_returns_rejection(self):
        engine = EntryEngine()
        m5 = MagicMock()
        m1 = MagicMock()
        h1 = MagicMock()
        result = engine.calculate_entry(
            pair="FAKEXYZ",
            direction="LONG",
            m5_df=m5,
            m1_df=m1,
            h1_df=h1,
            scan_result=_make_scan_result(pair="FAKEXYZ"),
            account_balance=10_000.0,
        )
        assert isinstance(result, EntryRejection)
        assert "not in registry" in result.reason.lower() or "unknown" in result.reason.lower()


# ═══════════════════════════════════════════════════════════════════════
# C. Startup: enabled pairs ⊄ INSTRUMENT_REGISTRY → abort
# ═══════════════════════════════════════════════════════════════════════

class TestStartupEnabledPairsValidation:

    def test_unresolved_override_fails_startup(self):
        checker = StartupCheck()
        _make_other_criticals_pass(checker)

        fake_cfg = MagicMock()
        fake_cfg.enabled_pairs = ["EURUSD", "TOTALLY_FAKE_SYMBOL"]

        with patch("config.AppConfig", return_value=fake_cfg):
            result = checker._check_instrument_registry()

        assert result.passed is False
        assert "TOTALLY_FAKE_SYMBOL" in result.message

    def test_all_valid_pairs_passes(self):
        checker = StartupCheck()
        _make_other_criticals_pass(checker)

        known = list(INSTRUMENT_REGISTRY.keys())[:3]
        fake_cfg = MagicMock()
        fake_cfg.enabled_pairs = known

        with patch("config.AppConfig", return_value=fake_cfg):
            result = checker._check_instrument_registry()

        assert result.passed is True

    def test_unresolved_pair_blocks_run_all(self):
        checker = StartupCheck()
        _make_other_criticals_pass(checker)

        fake_cfg = MagicMock()
        fake_cfg.enabled_pairs = ["EURUSD", "BOGUS_PAIR"]

        def _fake_registry_check():
            from config import INSTRUMENT_REGISTRY as reg, AppConfig
            _t0 = 0.0
            unresolved = [
                s for s in fake_cfg.enabled_pairs
                if s.upper().replace("/", "") not in reg
            ]
            if unresolved:
                return CheckResult(
                    name="instrument_registry",
                    passed=False,
                    message=f"Enabled pairs not in INSTRUMENT_REGISTRY: {unresolved}",
                    duration_ms=0.1,
                )
            return CheckResult(
                name="instrument_registry",
                passed=True,
                message="ok",
                duration_ms=0.1,
            )

        checker._check_instrument_registry = _fake_registry_check
        passed, results = checker.run_all()
        assert passed is False


# ═══════════════════════════════════════════════════════════════════════
# D. Non-positive / zero balance → RiskEngine rejects
# ═══════════════════════════════════════════════════════════════════════

class TestNonPositiveBalanceRejection:

    def test_zero_balance_rejected(self):
        engine = RiskEngine(starting_balance=0.0)
        result = engine.assess(
            pair="EURUSD",
            direction="LONG",
            entry_price=1.10000,
            stop_loss=1.09800,
            account_balance=0.0,
        )
        assert result.approved is False
        assert any("balance" in r.lower() for r in result.rejections)

    def test_none_balance_with_zero_seed_rejected(self):
        engine = RiskEngine(starting_balance=0.0)
        result = engine.assess(
            pair="EURUSD",
            direction="LONG",
            entry_price=1.10000,
            stop_loss=1.09800,
            account_balance=None,
        )
        assert result.approved is False
        assert any("balance" in r.lower() for r in result.rejections)

    def test_negative_balance_rejected(self):
        engine = RiskEngine(starting_balance=10_000.0)
        result = engine.assess(
            pair="EURUSD",
            direction="LONG",
            entry_price=1.10000,
            stop_loss=1.09800,
            account_balance=-500.0,
        )
        assert result.approved is False
        assert any("balance" in r.lower() for r in result.rejections)

    def test_positive_balance_passes(self):
        engine = RiskEngine(starting_balance=10_000.0)
        result = engine.assess(
            pair="EURUSD",
            direction="LONG",
            entry_price=1.10000,
            stop_loss=1.09800,
            account_balance=10_000.0,
            current_spread_pips=1.0,
        )
        assert not any("balance" in r.lower() for r in result.rejections)


# ═══════════════════════════════════════════════════════════════════════
# E. Weekly limit reads from config, not hardcoded 8.0
# ═══════════════════════════════════════════════════════════════════════

class TestWeeklyLimitFromConfig:

    def test_default_weekly_limit_is_8(self):
        cfg = RiskConfig()
        assert cfg.max_weekly_drawdown_pct == 8.0

    def test_custom_weekly_limit_respected(self):
        engine = RiskEngine(starting_balance=10_000.0)
        engine.risk_cfg.max_weekly_drawdown_pct = 3.0

        now = datetime.now(timezone.utc)
        for _ in range(5):
            engine.pnl_tracker.record(-100.0, False, now)

        with patch.object(engine.pnl_tracker, "is_daily_limit_hit", return_value=False):
            result = engine.assess(
                pair="EURUSD",
                direction="LONG",
                entry_price=1.10000,
                stop_loss=1.09800,
                account_balance=10_000.0,
                current_spread_pips=1.0,
            )
        has_weekly_recovery = any("Weekly loss" in c for c in result.checks)
        assert has_weekly_recovery

    def test_weekly_limit_in_snapshot_uses_config(self):
        engine = RiskEngine(starting_balance=10_000.0)
        engine.risk_cfg.max_weekly_drawdown_pct = 5.0
        snap = engine.get_account_snapshot(account_balance=10_000.0)
        expected_weekly_limit = 5.0 / 100.0 * 10_000.0
        assert snap.max_weekly_drawdown_remaining == expected_weekly_limit

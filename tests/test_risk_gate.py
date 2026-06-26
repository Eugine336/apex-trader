"""Tests for execution.risk_gate — Phase 6."""

from __future__ import annotations



from execution.intents import Intent
from execution.risk_gate import GateConfig, RiskGate


# ── Helpers ──────────────────────────────────────────────────────────

def _positions(**overrides) -> dict[str, dict]:
    """Build a minimal open_positions dict."""
    base = {
        "12345": {
            "symbol": "EURUSD",
            "direction": "BUY",
            "sl": 1.08000,
            "platform": "mt5",
            "lots": 0.10,
            "remaining_lots": 0.10,
        },
    }
    base["12345"].update(overrides)
    return base


def _close_intent(ticket: str = "12345") -> Intent:
    return Intent.close(symbol="EURUSD", ticket=ticket, source="test", reason="test")


def _sl_intent(ticket: str = "12345", new_sl: float = 1.08100) -> Intent:
    return Intent.modify_sl(
        symbol="EURUSD", ticket=ticket, new_sl=new_sl,
        source="test", reason="test",
    )


def _tp_intent(ticket: str = "12345", new_tp: float = 1.09000) -> Intent:
    return Intent.modify_tp(
        symbol="EURUSD", ticket=ticket, new_tp=new_tp,
        source="test", reason="test",
    )


def _partial_intent(ticket: str = "12345", fraction: float = 0.5) -> Intent:
    return Intent.partial_close(
        symbol="EURUSD", ticket=ticket, fraction=fraction,
        source="test", reason="test",
    )


# ── Position existence ───────────────────────────────────────────────

class TestPositionExists:
    def test_position_found(self):
        gate = RiskGate()
        result = gate.validate(_close_intent(), _positions())
        assert result.allowed

    def test_position_missing(self):
        gate = RiskGate()
        result = gate.validate(_close_intent("99999"), _positions())
        assert not result.allowed
        assert "99999" in result.reason
        assert "no longer open" in result.reason

    def test_empty_positions(self):
        gate = RiskGate()
        result = gate.validate(_close_intent(), {})
        assert not result.allowed


# ── SL direction ─────────────────────────────────────────────────────

class TestSLDirection:
    def test_tighter_sl_long_allowed(self):
        gate = RiskGate()
        result = gate.validate(
            _sl_intent(new_sl=1.08100),
            _positions(direction="BUY", sl=1.08000),
        )
        assert result.allowed

    def test_looser_sl_long_blocked(self):
        gate = RiskGate()
        result = gate.validate(
            _sl_intent(new_sl=1.07900),
            _positions(direction="BUY", sl=1.08000),
        )
        assert not result.allowed
        assert "loosening" in result.reason.lower()

    def test_tighter_sl_short_allowed(self):
        gate = RiskGate()
        result = gate.validate(
            _sl_intent(new_sl=1.07900),
            _positions(direction="SELL", sl=1.08000),
        )
        assert result.allowed

    def test_looser_sl_short_blocked(self):
        gate = RiskGate()
        result = gate.validate(
            _sl_intent(new_sl=1.08100),
            _positions(direction="SELL", sl=1.08000),
        )
        assert not result.allowed
        assert "loosening" in result.reason.lower()

    def test_sl_zero_allows_any(self):
        gate = RiskGate()
        result = gate.validate(
            _sl_intent(new_sl=1.07000),
            _positions(direction="BUY", sl=0.0),
        )
        assert result.allowed

    def test_modify_tp_skips_sl_check(self):
        gate = RiskGate()
        result = gate.validate(_tp_intent(), _positions())
        assert result.allowed

    def test_close_skips_sl_check(self):
        gate = RiskGate()
        result = gate.validate(_close_intent(), _positions())
        assert result.allowed


# ── Exposure / drawdown ──────────────────────────────────────────────

class TestExposure:
    def test_normal_drawdown_allows_modify(self):
        gate = RiskGate()
        result = gate.validate(_sl_intent(), _positions(), account_drawdown_pct=5.0)
        assert result.allowed

    def test_emergency_drawdown_blocks_modify(self):
        gate = RiskGate()
        result = gate.validate(_sl_intent(), _positions(), account_drawdown_pct=25.0)
        assert not result.allowed
        assert "emergency" in result.reason.lower()

    def test_emergency_drawdown_allows_close(self):
        gate = RiskGate()
        result = gate.validate(_close_intent(), _positions(), account_drawdown_pct=25.0)
        assert result.allowed

    def test_emergency_drawdown_allows_partial_close(self):
        gate = RiskGate()
        result = gate.validate(
            _partial_intent(), _positions(), account_drawdown_pct=25.0,
        )
        assert result.allowed

    def test_custom_threshold(self):
        gate = RiskGate(GateConfig(emergency_drawdown_pct=10.0))
        result = gate.validate(_sl_intent(), _positions(), account_drawdown_pct=12.0)
        assert not result.allowed


# ── Rate limit ───────────────────────────────────────────────────────

class TestRateLimit:
    def test_within_limit(self):
        gate = RiskGate(GateConfig(max_calls_per_second=10))
        for _ in range(10):
            result = gate.validate(_tp_intent(), _positions())
            assert result.allowed

    def test_exceeds_limit(self):
        gate = RiskGate(GateConfig(max_calls_per_second=3))
        results = []
        for _ in range(5):
            results.append(gate.validate(_tp_intent(), _positions()))
        rejected = [r for r in results if not r.allowed]
        assert len(rejected) >= 2
        assert "rate limit" in rejected[0].reason.lower()

    def test_rate_limit_disabled(self):
        gate = RiskGate(GateConfig(max_calls_per_second=0))
        for _ in range(20):
            result = gate.validate(_tp_intent(), _positions())
            assert result.allowed

    def test_risk_reducing_intents_never_throttled(self):
        # CLOSE / MODIFY_SL / PARTIAL_CLOSE protect the account and must never
        # be delayed by the rate budget, even when the symbol window is full.
        gate = RiskGate(GateConfig(max_calls_per_second=1))
        gate.validate(_tp_intent(), _positions())  # saturate EURUSD window
        assert not gate.validate(_tp_intent(), _positions()).allowed
        for intent in (
            _close_intent(),
            _sl_intent(new_sl=1.08100),
            _partial_intent(),
        ):
            assert gate.validate(intent, _positions()).allowed

    def test_rate_limit_is_per_symbol(self):
        # A flood on one symbol must not starve actions on another symbol.
        gate = RiskGate(GateConfig(max_calls_per_second=2))
        pos = _positions()
        pos["999"] = {
            "symbol": "GBPUSD", "direction": "BUY", "sl": 1.25000,
            "platform": "mt5", "lots": 0.10, "remaining_lots": 0.10,
        }
        for _ in range(2):
            assert gate.validate(_tp_intent(), pos).allowed
        assert not gate.validate(_tp_intent(), pos).allowed  # EURUSD full
        gbp_tp = Intent.modify_tp(
            symbol="GBPUSD", ticket="999", new_tp=1.30000,
            source="test", reason="test",
        )
        assert gate.validate(gbp_tp, pos).allowed  # GBPUSD has its own budget


# ── Integration ──────────────────────────────────────────────────────

class TestIntegration:
    def test_all_checks_pass(self):
        gate = RiskGate()
        result = gate.validate(
            _sl_intent(new_sl=1.08200),
            _positions(direction="BUY", sl=1.08000),
            account_drawdown_pct=3.0,
        )
        assert result.allowed

    def test_first_failing_check_short_circuits(self):
        gate = RiskGate()
        result = gate.validate(
            _sl_intent(ticket="99999", new_sl=1.07000),
            _positions(direction="BUY", sl=1.08000),
            account_drawdown_pct=25.0,
        )
        assert not result.allowed
        assert "no longer open" in result.reason

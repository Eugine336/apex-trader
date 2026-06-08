"""
APEX TRADER — Reconnect Drop-Detection Wiring Tests

Verifies that a mid-session broker connection drop is detected by the
live cycle and triggers the existing reconnect + reconciliation path.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

from platforms.platform_manager import PlatformManager
from platforms.trading_loop.recovery_mixin import RecoveryReconciliationMixin


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_manager(mt5_connected=True, deriv_connected=True):
    """Build a minimal PlatformManager with fake connectors."""
    mgr = PlatformManager.__new__(PlatformManager)
    mgr.config = MagicMock()
    mgr.mt5 = MagicMock()
    mgr.deriv = MagicMock()
    mgr.mt5_connectors = [mgr.mt5]
    mgr._mt5_connected_flags = [mt5_connected]
    mgr._deriv_connected = deriv_connected
    mgr._mt5_was_connected = [True]
    mgr._deriv_was_connected = True
    mgr._reconnect_delays = [5, 10, 20, 40, 60]
    mgr._mt5_reconnect_attempts = [0]
    mgr._deriv_reconnect_attempt = 0
    mgr._mt5_next_reconnects = [0.0]
    mgr._deriv_next_reconnect = 0.0
    return mgr


class _FakeLoop(RecoveryReconciliationMixin):
    """Minimal stand-in for TradingLoop, providing only what the mixin needs."""

    def __init__(self, platforms):
        self.platforms = platforms
        self.position_store = None
        self.managed_positions = {}
        self._reconcile_called = False

    def _reconcile_positions(self):
        self._reconcile_called = True


# ═══════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════

class TestDropDetectionTriggersReconnect:
    """The core regression: a dead socket must now be detected and reconnected."""

    def test_mt5_drop_detected_and_reconnect_attempted(self):
        mgr = _make_manager(mt5_connected=True, deriv_connected=True)
        mgr.mt5.is_connected.return_value = False
        mgr.deriv.is_connected.return_value = True
        mgr.mt5.connect.return_value = True

        loop = _FakeLoop(mgr)
        loop._check_and_reconnect()

        assert mgr._mt5_connected_flags[0] is True, "reconnect should have restored the flag"
        assert mgr._mt5_reconnect_attempts[0] == 0, "successful reconnect resets attempts"
        assert loop._reconcile_called, "_reconcile_positions must run after reconnect"

    def test_deriv_drop_detected_and_reconnect_attempted(self):
        mgr = _make_manager(mt5_connected=True, deriv_connected=True)
        mgr.mt5.is_connected.return_value = True
        mgr.deriv.is_connected.return_value = False
        mgr.deriv.connect.return_value = True

        loop = _FakeLoop(mgr)
        loop._check_and_reconnect()

        assert mgr._deriv_connected is True
        assert mgr._deriv_reconnect_attempt == 0
        assert loop._reconcile_called


class TestHealthyConnectionNoReconnect:
    """When both connectors remain live, no reconnect should fire."""

    def test_all_live_no_reconnect(self):
        mgr = _make_manager(mt5_connected=True, deriv_connected=True)
        mgr.mt5.is_connected.return_value = True
        mgr.deriv.is_connected.return_value = True

        loop = _FakeLoop(mgr)
        loop._check_and_reconnect()

        assert mgr._mt5_connected_flags[0] is True
        assert mgr._deriv_connected is True
        mgr.mt5.connect.assert_not_called()
        mgr.deriv.connect.assert_not_called()
        assert not loop._reconcile_called


class TestCheckConnectionsProbeFailureSurvives:
    """A crash inside check_connections must not kill the cycle."""

    def test_probe_exception_does_not_propagate(self):
        mgr = _make_manager(mt5_connected=True, deriv_connected=True)
        mgr.mt5.is_connected.side_effect = RuntimeError("socket gone")
        mgr.deriv.is_connected.return_value = True

        loop = _FakeLoop(mgr)
        loop._check_and_reconnect()


class TestReconnectThenReconcile:
    """On a successful reconnect after a detected drop, reconciliation runs."""

    def test_reconcile_called_after_reconnect(self):
        mgr = _make_manager(mt5_connected=True, deriv_connected=True)
        mgr.mt5.is_connected.return_value = False
        mgr.deriv.is_connected.return_value = True
        mgr.mt5.connect.return_value = True

        loop = _FakeLoop(mgr)
        loop._check_and_reconnect()

        assert loop._reconcile_called

    def test_reconcile_not_called_when_reconnect_fails(self):
        mgr = _make_manager(mt5_connected=True, deriv_connected=True)
        mgr.mt5.is_connected.return_value = False
        mgr.deriv.is_connected.return_value = True
        mgr.mt5.connect.return_value = False

        loop = _FakeLoop(mgr)
        loop._check_and_reconnect()

        assert not loop._reconcile_called

"""Tests for PortfolioGovernor daily-state persistence wiring in the
event-driven bootstrap (P6 dormant activation).

The governor's daily P&L tally + loss-cap halt previously lived only in
memory, so a restart inside the same UTC day reset the day's loss budget and
lifted any active halt. ``EventDrivenSystem._persist_governor_state`` /
``_restore_governor_state`` close that gap; these tests exercise the round
trip, the same-day guard, and the stale-day guard without standing up the
full system.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from event_driven_bootstrap import EventDrivenSystem
from governor import GovernorConfig, PortfolioGovernor


def _bare_system(governor, path):
    """Build an EventDrivenSystem shell with only the fields the persistence
    helpers touch (avoids the heavy real __init__)."""
    sys_obj = EventDrivenSystem.__new__(EventDrivenSystem)
    sys_obj._ctx = SimpleNamespace(portfolio_governor=governor)
    sys_obj._GOVERNOR_STATE_PATH = str(path)
    return sys_obj


def _halted_governor():
    gov = PortfolioGovernor(GovernorConfig())
    gov.set_reference_balance(10_000.0)
    # Drive a loss large enough to trip the daily loss-cap halt.
    gov.update_daily_pnl(-5_000.0)
    assert gov.daily_trading_halted is True
    return gov


def test_persist_restore_round_trip_same_day(tmp_path):
    path = tmp_path / "governor_state.json"
    gov = _halted_governor()
    saved_pnl = gov.daily_pnl

    _bare_system(gov, path)._persist_governor_state()
    assert path.exists()

    # Fresh governor (simulated restart) restores the halt + tally.
    fresh = PortfolioGovernor(GovernorConfig())
    assert fresh.daily_trading_halted is False
    _bare_system(fresh, path)._restore_governor_state()

    assert fresh.daily_trading_halted is True
    assert fresh.daily_pnl == saved_pnl


def test_restore_ignores_stale_prior_day(tmp_path):
    path = tmp_path / "governor_state.json"
    gov = _halted_governor()
    _bare_system(gov, path)._persist_governor_state()

    # Rewrite the persisted file as if it were written on a prior UTC day.
    payload = json.loads(path.read_text())
    payload["utc_day"] = "2000-01-01"
    path.write_text(json.dumps(payload))

    fresh = PortfolioGovernor(GovernorConfig())
    _bare_system(fresh, path)._restore_governor_state()

    # Yesterday's halt must NOT carry over — the day-roll reset owns that.
    assert fresh.daily_trading_halted is False
    assert fresh.daily_pnl == 0.0


def test_restore_missing_file_is_noop(tmp_path):
    path = tmp_path / "does_not_exist.json"
    fresh = PortfolioGovernor(GovernorConfig())
    _bare_system(fresh, path)._restore_governor_state()
    assert fresh.daily_trading_halted is False
    assert fresh.daily_pnl == 0.0


def test_persist_writes_current_utc_day(tmp_path):
    path = tmp_path / "governor_state.json"
    gov = PortfolioGovernor(GovernorConfig())
    gov.set_reference_balance(10_000.0)
    gov.update_daily_pnl(-100.0)

    _bare_system(gov, path)._persist_governor_state()
    payload = json.loads(path.read_text())

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert payload["utc_day"] == today
    assert payload["daily_pnl"] == -100.0

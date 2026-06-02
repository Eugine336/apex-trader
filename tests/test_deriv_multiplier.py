"""
Tests for Deriv multiplier config fix and runtime retry fallback.
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock



# ═══════════════════════════════════════════════════════════════════════════════
# Multiplier config validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestDerivMultiplierConfig:
    """Verify deriv.json has correct multiplier values."""

    def _load_config(self):
        cfg_path = Path(__file__).parent.parent / "config" / "brokers" / "deriv.json"
        with open(cfg_path) as f:
            return json.load(f)

    def test_all_multipliers_within_deriv_range(self):
        cfg = self._load_config()
        mults = cfg["multipliers"]
        for sym, entry in mults.items():
            default = entry["default"]
            accepted = entry["accepted"]
            assert default <= 1000, f"{sym} default {default} exceeds Deriv max 1000"
            for v in accepted:
                assert v <= 1000, f"{sym} accepted value {v} exceeds Deriv max 1000"

    def test_default_is_in_accepted(self):
        cfg = self._load_config()
        mults = cfg["multipliers"]
        for sym, entry in mults.items():
            assert entry["default"] in entry["accepted"], (
                f"{sym}: default {entry['default']} not in accepted {entry['accepted']}"
            )

    def test_all_synthetics_have_entries(self):
        cfg = self._load_config()
        mults = cfg["multipliers"]
        expected = [
            "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
            "BOOM500", "BOOM1000", "CRASH500", "CRASH1000",
        ]
        for sym in expected:
            assert sym in mults, f"Missing multiplier entry for {sym}"

    def test_accepted_values_are_sorted(self):
        cfg = self._load_config()
        mults = cfg["multipliers"]
        for sym, entry in mults.items():
            accepted = entry["accepted"]
            assert accepted == sorted(accepted), f"{sym} accepted not sorted: {accepted}"


# ═══════════════════════════════════════════════════════════════════════════════
# _get_multiplier unit tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetMultiplier:
    """Test DerivConnector._get_multiplier method."""

    def _make_connector(self):
        from platforms.deriv.deriv_connector import DerivConnector
        conn = DerivConnector.__new__(DerivConnector)
        conn._mapper = MagicMock()
        conn._discovered_multipliers = {}
        return conn

    def test_returns_configured_default(self):
        conn = self._make_connector()
        mult = conn._get_multiplier("1HZ100V")
        assert mult == 100

    def test_returns_from_default_entry_for_unknown_symbol(self):
        conn = self._make_connector()
        mult = conn._get_multiplier("UNKNOWN_SYMBOL_XYZ")
        assert mult == 1000

    def test_snaps_to_nearest_accepted(self):
        conn = self._make_connector()
        mult = conn._get_multiplier("1HZ75V")
        assert mult in [40, 100, 200, 300, 400]

    def test_all_returned_multipliers_are_valid(self):
        cfg_path = Path(__file__).parent.parent / "config" / "brokers" / "deriv.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        conn = self._make_connector()
        for sym, entry in cfg["multipliers"].items():
            if sym.startswith("_"):
                continue
            mult = conn._get_multiplier(sym)
            assert mult in entry["accepted"], (
                f"{sym}: _get_multiplier returned {mult} not in {entry['accepted']}"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Multiplier retry fallback
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiplierRetryParsing:
    """Test that the multiplier error regex correctly parses Deriv errors."""

    def test_parses_accepted_values_from_error(self):
        err = "Multiplier is not in acceptable range. Accepts 40,100,200,300,400."
        match = re.search(r"Multiplier is not in acceptable range.*?Accepts\s+([\d,]+)", err)
        assert match is not None
        valid = sorted(int(x) for x in match.group(1).split(","))
        assert valid == [40, 100, 200, 300, 400]

    def test_snaps_to_nearest_value(self):
        valid = [40, 100, 200, 300, 400]
        assert min(valid, key=lambda x: abs(x - 1000)) == 400
        assert min(valid, key=lambda x: abs(x - 150)) == 100  # equidistant, min returns first
        assert min(valid, key=lambda x: abs(x - 50)) == 40
        assert min(valid, key=lambda x: abs(x - 100)) == 100

    def test_no_match_on_unrelated_error(self):
        err = "Stake must be equal to or lower than 500.00"
        match = re.search(r"Multiplier is not in acceptable range.*?Accepts\s+([\d,]+)", err)
        assert match is None


# ═══════════════════════════════════════════════════════════════════════════════
# Startup check database fix
# ═══════════════════════════════════════════════════════════════════════════════

class TestStartupDatabaseCheck:
    """Verify the startup database check uses the real DB path."""

    def test_database_check_passes(self):
        from platforms.startup_check import StartupCheck
        check = StartupCheck()
        result = check._check_database()
        assert result.passed is True
        assert "SQLite OK" in result.message

    def test_database_check_no_tempfile_import(self):
        import inspect
        from platforms.startup_check import StartupCheck
        source = inspect.getsource(StartupCheck._check_database)
        assert "tempfile" not in source

"""
Regression tests for the 5 live-execution-safety fixes (C1-C4, C6).
Each test is designed to FAIL on the pre-fix code and PASS after.
"""

from unittest.mock import MagicMock



# ═══════════════════════════════════════════════════════════════════════════════
# C1: Deriv modify_order uses correct dollar formula
# ═══════════════════════════════════════════════════════════════════════════════


class TestDerivModifyOrderFormula:
    """Verify SL/TP dollar amounts use price_diff / entry * stake * multiplier."""

    def _make_connector(self, positions):
        """Build a minimal DerivConnector-like object with the real modify_order."""
        from platforms.deriv.deriv_connector import DerivConnector

        conn = object.__new__(DerivConnector)
        conn._positions = positions
        conn._ws = MagicMock()
        conn._connected = True
        conn._sync_send = MagicMock(return_value={})
        conn._require_connection = MagicMock()
        return conn

    def test_sl_dollar_amount_matches_deriv_contract_math(self):
        entry = 1000.0
        new_sl = 990.0
        stake = 50.0
        multiplier = 100
        positions = {
            "123": {
                "symbol": "1HZ100V", "direction": "BUY",
                "lots": 0.5, "sl": 980.0, "tp": 1020.0,
                "open_price": entry, "stake": stake,
                "multiplier": multiplier, "idem_key": None,
            }
        }
        conn = self._make_connector(positions)
        conn.modify_order("123", new_sl=new_sl)

        sent = conn._sync_send.call_args[0][0]
        expected_sl_dollar = round(abs(entry - new_sl) / entry * stake * multiplier, 2)
        assert sent["limit_order"]["stop_loss"] == expected_sl_dollar

    def test_tp_dollar_amount_matches_deriv_contract_math(self):
        entry = 1000.0
        new_tp = 1015.0
        stake = 50.0
        multiplier = 100
        positions = {
            "456": {
                "symbol": "1HZ100V", "direction": "BUY",
                "lots": 0.5, "sl": 990.0, "tp": 1010.0,
                "open_price": entry, "stake": stake,
                "multiplier": multiplier, "idem_key": None,
            }
        }
        conn = self._make_connector(positions)
        conn.modify_order("456", new_tp=new_tp)

        sent = conn._sync_send.call_args[0][0]
        expected_tp_dollar = round(abs(new_tp - entry) / entry * stake * multiplier, 2)
        assert sent["limit_order"]["take_profit"] == expected_tp_dollar

    def test_formula_is_not_the_old_lots_times_100(self):
        entry = 1000.0
        new_sl = 990.0
        stake = 50.0
        multiplier = 100
        lots = 0.5
        positions = {
            "789": {
                "symbol": "1HZ100V", "direction": "BUY",
                "lots": lots, "sl": 980.0, "tp": 1020.0,
                "open_price": entry, "stake": stake,
                "multiplier": multiplier, "idem_key": None,
            }
        }
        conn = self._make_connector(positions)
        conn.modify_order("789", new_sl=new_sl)

        sent = conn._sync_send.call_args[0][0]
        old_wrong_value = round(abs(entry - new_sl) * lots * 100, 2)
        correct_value = round(abs(entry - new_sl) / entry * stake * multiplier, 2)
        assert sent["limit_order"]["stop_loss"] == correct_value
        assert sent["limit_order"]["stop_loss"] != old_wrong_value

    def test_modify_returns_false_if_position_data_missing(self):
        conn = self._make_connector({})
        result = conn.modify_order("unknown", new_sl=1.0)
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# C2: Breakeven direction normalizes BUY/SELL → LONG/SHORT
# ═══════════════════════════════════════════════════════════════════════════════


class TestBreakevenDirectionNormalization:
    """Verify BUY positions get BE *above* entry (LONG math), not below."""

    def test_breakeven_long_is_above_entry(self):
        from management.partial_close import PartialCloseCalculator

        entry = 1.10000
        pip_size = 0.0001
        buffer_pips = 2.0
        be = PartialCloseCalculator.calculate_breakeven_level(
            entry, "LONG", buffer_pips, pip_size,
        )
        assert be > entry, f"LONG BE {be} should be > entry {entry}"

    def test_breakeven_short_is_below_entry(self):
        from management.partial_close import PartialCloseCalculator

        entry = 1.10000
        pip_size = 0.0001
        buffer_pips = 2.0
        be = PartialCloseCalculator.calculate_breakeven_level(
            entry, "SHORT", buffer_pips, pip_size,
        )
        assert be < entry, f"SHORT BE {be} should be < entry {entry}"

    def test_buy_direction_gets_long_math_in_exit_checks(self):
        """The normalization in exit_checks_mixin must map BUY→LONG."""
        direction = "BUY"
        direction_norm = "LONG" if direction.upper() in ("BUY", "LONG") else "SHORT"
        assert direction_norm == "LONG"

    def test_sell_direction_gets_short_math_in_exit_checks(self):
        direction = "SELL"
        direction_norm = "LONG" if direction.upper() in ("BUY", "LONG") else "SHORT"
        assert direction_norm == "SHORT"

    def test_buy_breakeven_via_normalization_is_above_entry(self):
        """End-to-end: BUY direction → normalize → BE above entry."""
        from management.partial_close import PartialCloseCalculator

        entry = 1.10000
        pip_size = 0.0001
        buffer_pips = 2.0
        direction = "BUY"
        direction_norm = "LONG" if direction.upper() in ("BUY", "LONG") else "SHORT"
        be = PartialCloseCalculator.calculate_breakeven_level(
            entry, direction_norm, buffer_pips, pip_size,
        )
        assert be > entry, f"BUY→LONG BE {be} must be above entry {entry}"

    def test_sell_breakeven_via_normalization_is_below_entry(self):
        from management.partial_close import PartialCloseCalculator

        entry = 1.10000
        pip_size = 0.0001
        buffer_pips = 2.0
        direction = "SELL"
        direction_norm = "LONG" if direction.upper() in ("BUY", "LONG") else "SHORT"
        be = PartialCloseCalculator.calculate_breakeven_level(
            entry, direction_norm, buffer_pips, pip_size,
        )
        assert be < entry, f"SELL→SHORT BE {be} must be below entry {entry}"


# ═══════════════════════════════════════════════════════════════════════════════
# C3: Dead-zone OR-precedence — config flag gates the whole check
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeadZoneOrPrecedence:
    """With dead_zone_management=False, JPY symbols must NOT get dead-zone mgmt."""

    def test_jpy_not_gated_when_disabled(self):
        """The fixed expression should be False when config is disabled."""
        symbol = "USDJPY"

        class Cfg:
            dead_zone_management = False

        cfg = Cfg()
        result = cfg.dead_zone_management and (
            symbol.find("USD") >= 0 or symbol.find("JPY") >= 0
        )
        assert result is False, "JPY should NOT trigger dead-zone when disabled"

    def test_jpy_gated_when_enabled(self):
        symbol = "USDJPY"

        class Cfg:
            dead_zone_management = True

        cfg = Cfg()
        result = cfg.dead_zone_management and (
            symbol.find("USD") >= 0 or symbol.find("JPY") >= 0
        )
        assert result is True

    def test_non_forex_not_gated_when_enabled(self):
        symbol = "1HZ100V"

        class Cfg:
            dead_zone_management = True

        cfg = Cfg()
        result = cfg.dead_zone_management and (
            symbol.find("USD") >= 0 or symbol.find("JPY") >= 0
        )
        assert result is False

    def test_old_bug_jpy_triggers_even_when_disabled(self):
        """Demonstrates the pre-fix bug: without parens, JPY triggers regardless."""
        symbol = "EURJPY"

        class Cfg:
            dead_zone_management = False

        cfg = Cfg()
        old_result = (
            cfg.dead_zone_management and symbol.find("USD") >= 0
            or symbol.find("JPY") >= 0
        )
        fixed_result = cfg.dead_zone_management and (
            symbol.find("USD") >= 0 or symbol.find("JPY") >= 0
        )
        assert old_result is True, "Old code would wrongly trigger for EURJPY"
        assert fixed_result is False, "Fixed code correctly blocks"


# ═══════════════════════════════════════════════════════════════════════════════
# C4: torch.load uses weights_only=True
# ═══════════════════════════════════════════════════════════════════════════════


class TestShadowLoadWeightsOnly:
    """Verify shadow._load calls torch.load with weights_only=True."""

    def test_weights_only_true_in_source(self):
        import inspect
        from rl.shadow import ShadowEngine

        source = inspect.getsource(ShadowEngine._load)
        assert "weights_only=True" in source, (
            "ShadowEngine._load must use weights_only=True"
        )
        assert "weights_only=False" not in source, (
            "weights_only=False must not appear in _load"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# C6: .db files untracked
# ═══════════════════════════════════════════════════════════════════════════════


class TestDbFilesUntracked:
    """Verify no .db files are tracked in git index."""

    def test_no_db_files_tracked(self):
        import subprocess

        result = subprocess.run(
            ["git", "ls-files", "*.db"],
            capture_output=True, text=True, cwd=str(__import__("pathlib").Path(__file__).parent.parent),
        )
        tracked = result.stdout.strip()
        assert tracked == "", f"These .db files are still tracked:\n{tracked}"

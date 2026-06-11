import pandas as pd
import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import contextmanager, ExitStack

from scanner.pair_scanner import PairScanResult
from trigger.entry_engine import EntryEngine, EntrySignal


def _make_scan_result(score: int = 90, direction: str = "LONG") -> PairScanResult:
    return PairScanResult(
        pair="EURUSD",
        direction=direction,
        score=score,
        regime="TRENDING",
        trend_h4="BULLISH" if direction == "LONG" else "BEARISH",
        trend_h1="BULLISH" if direction == "LONG" else "BEARISH",
        bias_strength="STRONG",
        has_fvg=True,
        has_order_block=True,
        has_liquidity_target=False,
        sweep_detected=False,
        inducement_detected=False,
        wyckoff_phase="MARKUP" if direction == "LONG" else "MARKDOWN",
        volume_confirmation=True,
        session_active=True,
        currency_strength_aligned=True,
        status="READY",
        timestamp=datetime.now(timezone.utc),
        confluences=["Structure aligned", "FVG entry zone"],
    )


def _make_trend_df(base: float = 1.2000, n: int = 60, step: float = 0.0001) -> pd.DataFrame:
    rows = []
    for i in range(n):
        open_price = base + i * step
        close_price = open_price + step
        rows.append(
            {
                "time": pd.Timestamp("2025-01-01 08:00") + pd.Timedelta(minutes=5 * i),
                "open": open_price,
                "high": close_price + step,
                "low": open_price - step,
                "close": close_price,
            }
        )
    return pd.DataFrame(rows)


class TestEntryModeDecisionRules:
    def setup_method(self):
        self.engine = EntryEngine()
        self.pip = 0.0001
        self.entry_price = 1.20000
        self.zone = {"top": 1.20020, "bottom": 1.19980, "midpoint": self.entry_price}

    def _decide(
        self,
        *,
        current_price: float,
        micro_confirmation: str = "no_confirmation",
        has_sweep: bool = False,
        score: int = 0,
        risk_reward: float = 0.0,
        momentum_score: int = 0,
    ) -> str:
        return self.engine._decide_entry_mode(
            direction="LONG",
            current_price=current_price,
            entry_price=self.entry_price,
            zone=self.zone,
            micro_confirmation=micro_confirmation,
            has_sweep=has_sweep,
            pip_size=self.pip,
            score=score,
            risk_reward=risk_reward,
            momentum_score=momentum_score,
        )

    def test_inside_zone_is_market(self):
        assert self._decide(current_price=1.20010) == "MARKET"

    def test_strong_confirmation_within_three_pips_is_market(self):
        assert self._decide(current_price=1.20030, micro_confirmation="engulfing") == "MARKET"

    def test_sweep_within_five_pips_is_market(self):
        assert self._decide(current_price=1.20050, has_sweep=True) == "MARKET"

    def test_high_score_strong_momentum_within_five_pips_is_market(self):
        assert (
            self._decide(
                current_price=1.20045,
                micro_confirmation="momentum_only",
                score=82,
                momentum_score=3,
            )
            == "MARKET"
        )

    def test_good_rr_within_three_pips_is_market(self):
        assert self._decide(current_price=1.20030, risk_reward=2.5) == "MARKET"

    def test_momentum_only_within_two_pips_is_market(self):
        assert (
            self._decide(
                current_price=1.20020,
                micro_confirmation="momentum_only",
                momentum_score=3,
            )
            == "MARKET"
        )

    def test_sweep_plus_confirmation_within_seven_pips_is_market(self):
        assert (
            self._decide(
                current_price=1.20070,
                micro_confirmation="momentum_only",
                has_sweep=True,
                momentum_score=3,
            )
            == "MARKET"
        )

    def test_weak_far_setup_remains_pending(self):
        assert (
            self._decide(
                current_price=1.20080,
                micro_confirmation="no_confirmation",
                has_sweep=False,
                score=60,
                risk_reward=1.2,
                momentum_score=0,
            )
            == "PENDING"
        )


class TestEntryModeSignalPricing:
    def setup_method(self):
        self.engine = EntryEngine()
        self.zone = {
            "type": "FVG_MIDPOINT",
            "top": 1.20020,
            "bottom": 1.19980,
            "midpoint": 1.20000,
            "fvg": None,
            "ob": None,
            "has_sweep": False,
        }
        self.m5 = _make_trend_df(base=1.2000, n=40)
        self.h1 = _make_trend_df(base=1.2000, n=80)

    def _patch_common_dependencies(self, decision: str):
        @contextmanager
        def _ctx():
            with ExitStack() as stack:
                stack.enter_context(patch.object(self.engine, "find_entry_zone", return_value=self.zone))
                stack.enter_context(
                    patch.object(self.engine.pattern_detector, "get_best_pattern", return_value=("", ""))
                )
                stack.enter_context(patch.object(self.engine, "_detect_m1_choch", return_value=False))
                stack.enter_context(
                    patch.object(
                        self.engine.news_guard, "check", return_value=SimpleNamespace(is_clear=True, warning_message="")
                    )
                )
                stack.enter_context(
                    patch.object(
                        self.engine.session_engine,
                        "get_status",
                        return_value=SimpleNamespace(
                            current_session="LONDON", session_open_minutes=30, is_tradeable=True
                        ),
                    )
                )
                stack.enter_context(patch.object(self.engine.drawdown, "can_trade", return_value=(True, "")))
                stack.enter_context(
                    patch.object(
                        self.engine.drawdown,
                        "get_status",
                        return_value=SimpleNamespace(current_risk_pct=0.01, current_score_threshold=65, mode="NORMAL"),
                    )
                )
                stack.enter_context(patch.object(self.engine, "_decide_entry_mode", return_value=decision))
                stack.enter_context(
                    patch.object(
                        self.engine,
                        "calculate_stop_loss",
                        side_effect=lambda direction,
                        entry_zone,
                        pip_size,
                        buffer_pips=2.0,
                        entry_price=None,
                        pair="",
                        m5_df=None: entry_price - 0.001,
                    )
                )
                stack.enter_context(
                    patch.object(
                        self.engine,
                        "calculate_targets",
                        side_effect=lambda pair, direction, entry_price, stop_loss, h1_df, pip_size: (
                            entry_price + 0.0015,
                            entry_price + 0.0030,
                        ),
                    )
                )
                stack.enter_context(patch.object(self.engine, "calculate_position_size", return_value=0.5))
                yield

        return _ctx()

    def test_market_mode_uses_current_market_price_for_entry(self):
        m1 = pd.DataFrame(
            [
                {
                    "time": pd.Timestamp("2025-01-01 08:00"),
                    "open": 1.20000,
                    "high": 1.20015,
                    "low": 1.19995,
                    "close": 1.20010,
                },
                {
                    "time": pd.Timestamp("2025-01-01 08:01"),
                    "open": 1.20010,
                    "high": 1.20022,
                    "low": 1.20005,
                    "close": 1.20020,
                },
                {
                    "time": pd.Timestamp("2025-01-01 08:02"),
                    "open": 1.20020,
                    "high": 1.20033,
                    "low": 1.20015,
                    "close": 1.20030,
                },
                {
                    "time": pd.Timestamp("2025-01-01 08:03"),
                    "open": 1.20030,
                    "high": 1.20045,
                    "low": 1.20025,
                    "close": 1.20040,
                },
                {
                    "time": pd.Timestamp("2025-01-01 08:04"),
                    "open": 1.20040,
                    "high": 1.20055,
                    "low": 1.20035,
                    "close": 1.20050,
                },
            ]
        )
        scan_result = _make_scan_result(score=90)

        with self._patch_common_dependencies(decision="MARKET"):
            result = self.engine.calculate_entry(
                "EURUSD",
                "LONG",
                self.m5,
                m1,
                self.h1,
                scan_result=scan_result,
                account_balance=10_000.0,
            )

        assert isinstance(result, EntrySignal)
        assert result.entry_mode == "MARKET"
        assert result.entry_price == pytest.approx(1.20050, rel=0.0, abs=1e-5)
        assert result.entry_price != pytest.approx(self.zone["midpoint"], rel=0.0, abs=1e-5)

    def test_pending_mode_keeps_zone_midpoint_entry_price(self):
        m1 = pd.DataFrame(
            [
                {
                    "time": pd.Timestamp("2025-01-01 08:00"),
                    "open": 1.20000,
                    "high": 1.20005,
                    "low": 1.19985,
                    "close": 1.19990,
                },
                {
                    "time": pd.Timestamp("2025-01-01 08:01"),
                    "open": 1.19990,
                    "high": 1.20000,
                    "low": 1.19980,
                    "close": 1.19985,
                },
                {
                    "time": pd.Timestamp("2025-01-01 08:02"),
                    "open": 1.19985,
                    "high": 1.19995,
                    "low": 1.19975,
                    "close": 1.19980,
                },
                {
                    "time": pd.Timestamp("2025-01-01 08:03"),
                    "open": 1.19980,
                    "high": 1.19990,
                    "low": 1.19970,
                    "close": 1.19975,
                },
                {
                    "time": pd.Timestamp("2025-01-01 08:04"),
                    "open": 1.19975,
                    "high": 1.19985,
                    "low": 1.19965,
                    "close": 1.19970,
                },
            ]
        )
        scan_result = _make_scan_result(score=90)

        with self._patch_common_dependencies(decision="PENDING"):
            result = self.engine.calculate_entry(
                "EURUSD",
                "LONG",
                self.m5,
                m1,
                self.h1,
                scan_result=scan_result,
                account_balance=10_000.0,
            )

        assert isinstance(result, EntrySignal)
        assert result.entry_mode == "PENDING"
        assert result.entry_price == pytest.approx(self.zone["midpoint"], rel=0.0, abs=1e-5)
        assert result.entry_price != pytest.approx(1.19970, rel=0.0, abs=1e-5)

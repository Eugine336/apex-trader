"""
Test for #9 (part 1) — recency windowing of learner training data.

`AdaptiveOptimizer._recent_trades` trains the learners on recent trades so they
adapt to the current regime, with a min-sample fallback so a young account never
starves the learners.
"""

from datetime import datetime, timedelta, timezone

from adaptive.optimizer import AdaptiveOptimizer


def _t(days_ago: float) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"timestamp": ts, "pnl": 1.0}


def test_disabled_window_returns_all():
    trades = [_t(i) for i in range(100)]
    assert AdaptiveOptimizer._recent_trades(trades, 0, 50) is trades


def test_small_history_never_windowed():
    trades = [_t(i) for i in range(10)]  # <= min_trades → keep full
    assert AdaptiveOptimizer._recent_trades(trades, 90, 50) == trades


def test_windows_when_enough_recent():
    recent = [_t(i) for i in range(60)]          # within 90 days
    old = [_t(200 + i) for i in range(60)]       # older than 90 days
    out = AdaptiveOptimizer._recent_trades(recent + old, 90, 50)
    assert len(out) == 60
    assert all(
        (datetime.now(timezone.utc) - AdaptiveOptimizer._parse_trade_ts(t)).days < 90
        for t in out
    )


def test_fallback_when_recent_too_few():
    recent = [_t(i) for i in range(10)]          # only 10 recent (< 50)
    old = [_t(200 + i) for i in range(100)]
    trades = recent + old
    # Not enough recent → use the full history rather than starve the learners.
    assert AdaptiveOptimizer._recent_trades(trades, 90, 50) is trades


def test_parse_ts_handles_missing_and_bad():
    assert AdaptiveOptimizer._parse_trade_ts({}) is None
    assert AdaptiveOptimizer._parse_trade_ts({"timestamp": "not-a-date"}) is None
    assert AdaptiveOptimizer._parse_trade_ts({"timestamp": "2026-01-01T00:00:00Z"}) is not None

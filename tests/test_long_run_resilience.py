"""
APEX TRADER — Long-Run Resilience Tests

Covers the four bottlenecks hardened for unattended 1-year operation:

1. SQLite WAL growth + periodic VACUUM   (DailyMaintenance._vacuum_databases)
2. Git data-repo compaction              (backup_data.compact_repo_history)
3. Memory watchdog + restart marker      (DailyMaintenance._memory_maintenance)
4. Recency time-decay weighting          (AdaptiveOptimizer._recent_trades + learners)
"""

import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from platforms.maintenance import DailyMaintenance
from adaptive.optimizer import AdaptiveOptimizer
from adaptive.pair_learner import PairLearner
from adaptive.recency_weight import (
    RECENCY_WEIGHT_KEY,
    trade_weight,
    weighted_mean,
    weighted_win_rate,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_sqlite_db(path: Path, rows: int = 0) -> None:
    """Create a real SQLite DB so VACUUM/checkpoint operate on a valid file."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, blob TEXT)")
        if rows:
            conn.executemany(
                "INSERT INTO t (blob) VALUES (?)",
                [("x" * 256,) for _ in range(rows)],
            )
            conn.commit()
            # Delete to leave reclaimable free pages (what VACUUM reclaims).
            conn.execute("DELETE FROM t")
            conn.commit()
    finally:
        conn.close()


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.io"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)


def _make_trade(pnl: float, *, pair: str = "EURUSD", regime: str = "TRENDING",
                session: str = "LONDON", ts: datetime | None = None) -> dict:
    t = {"pair": pair, "pnl": pnl, "regime": regime, "session": session}
    if ts is not None:
        t["timestamp"] = ts.isoformat()
    return t


# ═══════════════════════════════════════════════════════════════════════════
# FIX 1 — VACUUM
# ═══════════════════════════════════════════════════════════════════════════


class TestVacuumDatabases:
    def test_vacuums_large_skips_small(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            big = data_dir / "big.db"
            small = data_dir / "small.db"
            _make_sqlite_db(big, rows=2000)   # well over a few KB
            _make_sqlite_db(small, rows=0)    # tiny schema-only file

            # Threshold between the two file sizes.
            big_mb = big.stat().st_size / (1024 * 1024)
            small_mb = small.stat().st_size / (1024 * 1024)
            assert big_mb > small_mb
            threshold = (big_mb + small_mb) / 2.0

            m = DailyMaintenance(
                data_dir=str(data_dir), vacuum_size_threshold_mb=threshold
            )
            with patch("persistence.event_store.get_event_store") as ges:
                summary = m._vacuum_databases()

            ges.assert_called_once()
            assert summary["event_store"] == "ok"
            assert summary["checked"] == 2          # both non-event-store dbs
            assert summary["vacuumed"] == 1          # only the big one

    def test_no_data_dir_is_safe(self):
        m = DailyMaintenance(data_dir="/nope/does/not/exist")
        with patch("persistence.event_store.get_event_store"):
            summary = m._vacuum_databases()
        assert summary["checked"] == 0
        assert summary["vacuumed"] == 0

    def test_event_store_failure_non_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            m = DailyMaintenance(data_dir=str(data_dir))
            with patch(
                "persistence.event_store.get_event_store",
                side_effect=RuntimeError("boom"),
            ):
                summary = m._vacuum_databases()
            assert "error" in summary["event_store"]

    def test_run_includes_vacuum_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            m = DailyMaintenance(
                data_dir=str(data_dir), log_dir=str(Path(tmp) / "logs")
            )
            with patch("persistence.event_store.get_event_store"):
                result = m.run()
            assert "databases_vacuumed" in result
            assert "memory" in result


# ═══════════════════════════════════════════════════════════════════════════
# FIX 2 — Repo compaction
# ═══════════════════════════════════════════════════════════════════════════


class TestRepoCompaction:
    def test_not_a_git_repo(self):
        from scripts.backup_data import compact_repo_history
        with tempfile.TemporaryDirectory() as tmp:
            assert compact_repo_history(Path(tmp)) == "not a git repo"

    def test_missing_dir(self):
        from scripts.backup_data import compact_repo_history
        assert compact_repo_history("/nope/missing") == "no data directory"

    def test_under_threshold_noop(self):
        from scripts.backup_data import compact_repo_history
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _init_repo(d)
            (d / "f.db").write_text("x")
            subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
            # Huge threshold → never triggers.
            res = compact_repo_history(d, max_repo_size_mb=100_000.0)
            assert res.startswith("ok")

    def test_squashes_history_when_over_threshold(self):
        from scripts.backup_data import compact_repo_history
        with tempfile.TemporaryDirectory() as tmp:
            remote = Path(tmp) / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            d = Path(tmp) / "work"
            d.mkdir()
            _init_repo(d)
            subprocess.run(
                ["git", "-C", str(d), "remote", "add", "origin", str(remote)],
                check=True,
            )
            # Several commits so there is real history to squash.
            for i in range(5):
                (d / "f.db").write_text(f"rev{i}")
                subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
                subprocess.run(
                    ["git", "-C", str(d), "commit", "-qm", f"c{i}"], check=True
                )
            before = int(subprocess.check_output(
                ["git", "-C", str(d), "rev-list", "--count", "HEAD"], text=True
            ).strip())
            assert before == 5

            # Force the trigger: zero size threshold, keep only 2 commits.
            res = compact_repo_history(
                d, max_repo_size_mb=0.0, keep_commits=2, branch="master",
            )
            assert "compacted" in res

            after = int(subprocess.check_output(
                ["git", "-C", str(d), "rev-list", "--count", "HEAD"], text=True
            ).strip())
            assert after == 1
            # Current tree preserved.
            assert (d / "f.db").read_text() == "rev4"

    def test_keep_commits_guards_small_history(self):
        from scripts.backup_data import compact_repo_history
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _init_repo(d)
            (d / "f.db").write_text("x")
            subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
            # Over size threshold but only 1 commit ≤ keep_commits → skipped.
            res = compact_repo_history(d, max_repo_size_mb=0.0, keep_commits=10)
            assert res.startswith("skipped")


# ═══════════════════════════════════════════════════════════════════════════
# FIX 3 — Memory maintenance
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoryMaintenance:
    def test_runs_gc_and_reports_rss(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            m = DailyMaintenance(data_dir=str(data_dir), max_rss_mb=10_000.0)
            with patch("ops.lifecycle._process_memory_mb", return_value=123.4):
                out = m._memory_maintenance()
            assert "collected" in out
            assert isinstance(out["collected"], int)
            assert out["rss_mb"] == 123.4
            # Under ceiling → no restart marker.
            assert not (data_dir / ".restart_requested").exists()

    def test_writes_restart_marker_over_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            m = DailyMaintenance(data_dir=str(data_dir), max_rss_mb=100.0)
            with patch("ops.lifecycle._process_memory_mb", return_value=5000.0):
                out = m._memory_maintenance()
            assert out.get("restart_requested") is True
            marker = data_dir / ".restart_requested"
            assert marker.exists()
            assert "memory_pressure" in marker.read_text()

    def test_unreadable_rss_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            m = DailyMaintenance(data_dir=str(data_dir), max_rss_mb=1.0)
            with patch("ops.lifecycle._process_memory_mb", return_value=None):
                out = m._memory_maintenance()
            assert out["rss_mb"] is None
            # No RSS → cannot breach ceiling → no marker.
            assert not (data_dir / ".restart_requested").exists()


# ═══════════════════════════════════════════════════════════════════════════
# FIX 4 — Recency weighting
# ═══════════════════════════════════════════════════════════════════════════


class TestRecencyWeightHelpers:
    def test_trade_weight_defaults_and_guards(self):
        assert trade_weight({}) == 1.0
        assert trade_weight({RECENCY_WEIGHT_KEY: 0.5}) == 0.5
        assert trade_weight({RECENCY_WEIGHT_KEY: "bad"}) == 1.0
        assert trade_weight({RECENCY_WEIGHT_KEY: -3}) == 1.0
        assert trade_weight({RECENCY_WEIGHT_KEY: float("nan")}) == 1.0

    def test_weighted_win_rate_excludes_scratches(self):
        pnls = [10.0, -5.0, 0.0]
        weights = [1.0, 1.0, 1.0]
        assert weighted_win_rate(pnls, weights) == 0.5  # scratch excluded
        # Down-weighting the loss raises the win rate.
        assert weighted_win_rate([10.0, -5.0], [1.0, 0.0]) == 1.0

    def test_weighted_mean(self):
        assert weighted_mean([10.0, 0.0], [3.0, 1.0]) == 7.5
        assert weighted_mean([], []) == 0.0


class TestRecentTradesWeighting:
    def test_disabled_window_returns_unchanged(self):
        trades = [_make_trade(5.0)]
        out = AdaptiveOptimizer._recent_trades(trades, window_days=0, min_trades=50)
        assert out is trades
        assert RECENCY_WEIGHT_KEY not in out[0]

    def test_fallback_adds_weights_old_decays(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        recent_ts = now - timedelta(days=10)     # inside 90d window
        old_ts = now - timedelta(days=300)        # far outside → decayed
        trades = [
            _make_trade(5.0, ts=recent_ts),
            _make_trade(-5.0, ts=old_ts),
        ]
        # Only 2 trades, min_trades=50 → fallback to full history with weights.
        out = AdaptiveOptimizer._recent_trades(
            trades, window_days=90, min_trades=50, now=now, half_life_days=60.0,
        )
        assert all(RECENCY_WEIGHT_KEY in t for t in out)
        # Input dicts never mutated.
        assert RECENCY_WEIGHT_KEY not in trades[0]
        w_recent = out[0][RECENCY_WEIGHT_KEY]
        w_old = out[1][RECENCY_WEIGHT_KEY]
        assert w_recent == 1.0           # within window
        assert 0.0 < w_old < 1.0          # decayed
        assert w_old < w_recent

    def test_enough_recent_skips_weighting(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        recent_ts = now - timedelta(days=1)
        trades = [_make_trade(1.0, ts=recent_ts) for _ in range(60)]
        out = AdaptiveOptimizer._recent_trades(
            trades, window_days=90, min_trades=50, now=now,
        )
        # All within window and ≥ min_trades → returned as the recent window,
        # no weight field needed.
        assert len(out) == 60
        assert all(RECENCY_WEIGHT_KEY not in t for t in out)


class TestLearnerWeighting:
    def test_pair_learner_respects_weights(self):
        # Old losers (down-weighted) + recent winners. Weighted win rate should
        # exceed the naive unweighted win rate.
        trades = (
            [{"pair": "EURUSD", "pnl": 10.0, RECENCY_WEIGHT_KEY: 1.0}] * 6
            + [{"pair": "EURUSD", "pnl": -10.0, RECENCY_WEIGHT_KEY: 0.01}] * 6
        )
        learner = PairLearner()
        profiles = learner.learn(trades)
        weighted_wr = profiles["EURUSD"].win_rate

        # Same trades, no weights → naive 50%.
        plain = [{"pair": "EURUSD", "pnl": t["pnl"]} for t in trades]
        learner2 = PairLearner()
        plain_wr = learner2.learn(plain)["EURUSD"].win_rate

        assert abs(plain_wr - 0.5) < 1e-6
        assert weighted_wr > plain_wr      # losses faded → higher win rate

    def test_pair_learner_all_unit_weights_matches_unweighted(self):
        trades = (
            [{"pair": "GBPUSD", "pnl": 8.0, RECENCY_WEIGHT_KEY: 1.0}] * 5
            + [{"pair": "GBPUSD", "pnl": -4.0, RECENCY_WEIGHT_KEY: 1.0}] * 5
        )
        learner = PairLearner()
        weighted = learner.learn(trades)["GBPUSD"]

        plain = [{"pair": "GBPUSD", "pnl": t["pnl"]} for t in trades]
        learner2 = PairLearner()
        unweighted = learner2.learn(plain)["GBPUSD"]

        assert abs(weighted.win_rate - unweighted.win_rate) < 1e-6
        assert abs(weighted.avg_pnl - unweighted.avg_pnl) < 1e-6

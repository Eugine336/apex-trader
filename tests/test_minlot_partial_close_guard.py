"""Executor min-lot behavior and broker-volume snapshot normalization.

The PositionWorker no longer creates discretionary TP1 partial-close intents.
This suite therefore validates the shared executor's min-lot contract and
broker volume normalization only; it does not test a retired TP1 worker path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from execution.action_executor import ActionExecutor, ExecutorConfig
from execution.intents import Intent
from execution.risk_gate import GateConfig

# ── Shared recording fake broker (never connects to a live platform) ─────

from test_support.recording_broker import FakeCloseResult as _FakeCloseResult
from test_support.recording_broker import RecordingFakeBroker as _RecordingFakeBroker

class _FakeBroker(_RecordingFakeBroker):
    def __init__(self):
        super().__init__()
        self.close_return = _FakeCloseResult(pnl=0.0)


def _fast_cfg() -> ExecutorConfig:
    return ExecutorConfig(
        max_retries=1,
        retry_base_delay_s=0.001,
        circuit_failure_threshold=10,
        circuit_cooldown_s=0.1,
        gate_config=GateConfig(max_calls_per_second=1000),
    )


def _positions(
    remaining_lots: float,
    volume_step: float = 0.01,
    volume_min: float = 0.01,
) -> dict[str, dict]:
    return {
        "T1": {
            "symbol": "GBPUSD",
            "direction": "SELL",
            "sl": 1.32300,
            "platform": "mt5",
            "lots": remaining_lots,
            "remaining_lots": remaining_lots,
            "volume_step": volume_step,
            "volume_min": volume_min,
        },
    }


def _partial_intent(fraction: float = 0.5) -> Intent:
    return Intent.partial_close(
        symbol="GBPUSD",
        ticket="T1",
        fraction=fraction,
        source="test",
        reason="test",
    )


# ── Snapshot field ───────────────────────────────────────────────────


class TestSnapshotVolumeMin:
    def test_default_volume_min(self):
        from types import SimpleNamespace

        from execution.position_snapshot import build_position_snapshot

        pos = SimpleNamespace(
            order_id="T1",
            platform="mt5",
            symbol="EURUSD",
            direction="BUY",
            open_price=1.10,
            lots=0.01,
            sl=1.099,
            tp=1.11,
            tp2=1.12,
        )
        snap = build_position_snapshot(pos, current_price=1.105)
        assert snap.volume_min == 0.01

    def test_volume_min_from_pos(self):
        from types import SimpleNamespace

        from execution.position_snapshot import build_position_snapshot

        pos = SimpleNamespace(
            order_id="T1",
            platform="mt5",
            symbol="DE40",
            direction="BUY",
            open_price=24000.0,
            lots=0.1,
            sl=23900.0,
            tp=24100.0,
            tp2=24200.0,
            volume_min=0.1,
        )
        snap = build_position_snapshot(pos, current_price=24050.0)
        assert snap.volume_min == 0.1

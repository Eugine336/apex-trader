from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from execution.lifecycle_loops import FlushLoop


def test_flush_position_map_routes_mt5_ticket_to_originating_connector():
    loop = FlushLoop.__new__(FlushLoop)
    loop._pm = SimpleNamespace(
        get_all_open_positions=lambda: [
            SimpleNamespace(
                order_id="ticket-1001",
                symbol="EURUSD",
                direction="BUY",
                sl=1.08,
                platform="mt5",
                lots=0.1,
                remaining_lots=0.1,
                volume_min=0.01,
                volume_step=0.01,
                volume_max=100.0,
                digits=5,
                tick_size=0.00001,
                account_key="Pepperstone:2002",
                connector_index=1,
            ),
        ],
    )

    positions = loop._build_position_map()

    assert positions["ticket-1001"]["platform"] == "mt5_1"
    assert positions["ticket-1001"]["account_key"] == "Pepperstone:2002"
    assert positions["ticket-1001"]["connector_index"] == 1


def test_flush_position_map_keeps_deriv_route_without_mt5_index():
    loop = FlushLoop.__new__(FlushLoop)
    loop._pm = SimpleNamespace(
        get_all_open_positions=lambda: [
            SimpleNamespace(
                order_id="deriv-ticket",
                symbol="R_100",
                direction="SELL",
                sl=0.0,
                platform="deriv",
                lots=1.0,
                account_key="deriv:VRTC12345",
                connector_index=0,
            ),
        ],
    )

    positions = loop._build_position_map()

    assert positions["deriv-ticket"]["platform"] == "deriv"

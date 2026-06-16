from platforms.deriv.deriv_connector import DerivConnector
from unittest.mock import MagicMock, patch

class TestDerivStakeCapRetry:
    def _build_connector(self, responses):
        conn = DerivConnector.__new__(DerivConnector)
        conn._connected = True
        conn._reconnecting = False
        conn._ws = MagicMock()
        conn._positions = {}
        conn._discovered_multipliers = {}
        conn._mapper = MagicMock()
        conn._mapper.to_broker.return_value = '1HZ10V'
        conn._thread_lock = __import__('threading').Lock()
        conn._last_history_request = 0.0
        conn._max_tick_age_seconds = 120.0

        call_count = {'n': 0}

        def fake_sync_send(payload):
            if 'ticks_history' in payload:
                return {
                    'history': {
                        'prices': [10200.0],
                        'times': [int(__import__('time').time())],
                    }
                }
            idx = call_count['n']
            call_count['n'] += 1
            if idx < len(responses):
                return responses[idx]
            return responses[-1]

        conn._sync_send = fake_sync_send
        return conn

if __name__ == '__main__':
    responses = [
        {'error': {'message': 'Input validation failed: parameters'}},
        {'buy': {'contract_id': '123', 'buy_price': 47.00}},
    ]
    conn = TestDerivStakeCapRetry()._build_connector(responses)
    with patch.object(conn, '_get_multiplier', return_value=1000):
        result = conn.place_order('V10_1S', 'LONG', 0.01, sl=10176.0, tp=10250.0, stake_usd=47.54)
    print('RESULT', result.success, result.order_id, result.error)

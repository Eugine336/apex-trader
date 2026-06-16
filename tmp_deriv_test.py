from platforms.deriv.deriv_connector import DerivConnector
from unittest.mock import patch
from datetime import datetime, timezone

responses = [
    {
        'history': {
            'prices': [10200.0],
            'times': [int(datetime.now(timezone.utc).timestamp())],
        }
    },
    {'error': {'message': 'Input validation failed: parameters'}},
    {'buy': {'contract_id': '123', 'buy_price': 47.0}},
]
call_count = {'n': 0}

def fake_sync_send(payload):
    print('SEND', payload)
    if 'ticks_history' in payload:
        return responses[0]
    idx = call_count['n']
    call_count['n'] += 1
    return responses[idx + 1]

conn = DerivConnector(client_id='x', access_token='x', app_id='x')
conn._connected = True
conn._sync_send = fake_sync_send
with patch.object(conn, '_get_multiplier', return_value=1000):
    result = conn.place_order('V10_1S', 'SHORT', 0.01, sl=10176.0, tp=10250.0, stake_usd=47.54)
    print('RESULT', result.success, result.order_id, result.error)

# ChatGPT → APEX → Binance Executor

This integration treats the ChatGPT opportunity-harvesting session as the trade-decision source and APEX as the execution boundary. APEX does not reinterpret the ChatGPT thesis.

## Modes

`PAPER` is the default and requires no Binance credentials.

`LIVE` requires all of:

- `BINANCE_EXECUTION_MODE=LIVE`
- `BINANCE_LIVE_EXECUTION_ENABLED=true`
- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`

The API key should have only the permissions required for Futures trading. **Do not enable withdrawals or transfers.** Store credentials only in the runtime environment/secret store; never commit them.

## Start

```powershell
python -m pip install -r integrations/chatgpt_signals/requirements.txt
$env:CHATGPT_RELAY_TOKEN = '<long-random-secret>'
python -m integrations.chatgpt_signals.relay_server
```

## Paper test

Leave `BINANCE_EXECUTION_MODE=PAPER`. Send a signal with:

```json
{
  "signal_id": "test-001",
  "symbol": "BTCUSDT",
  "side": "BUY",
  "order_type": "MARKET",
  "quantity": 0.001,
  "stop_loss": 72000,
  "take_profit": 73000,
  "expires_at": "2099-01-01T00:00:00Z",
  "mode": "PAPER"
}
```

## Live mode

Do not enable LIVE until the complete paper relay has been tested. When deliberately enabling it, use:

```powershell
$env:BINANCE_EXECUTION_MODE='LIVE'
$env:BINANCE_LIVE_EXECUTION_ENABLED='true'
$env:BINANCE_API_KEY='...'
$env:BINANCE_API_SECRET='...'
$env:BINANCE_MAX_NOTIONAL_USDT='100'
python -m integrations.chatgpt_signals.relay_server
```

The live adapter validates the symbol and trading status, reads current bid/ask, rounds quantity and prices to exchange filters, enforces the configured notional ceiling, validates stop/target geometry, submits a MARKET entry, then immediately submits protective STOP_MARKET and TAKE_PROFIT_MARKET close-position orders. It records order IDs and fill data in the local execution ledger.

If entry succeeds but a protective order fails, the process raises an explicit error and does not hide the fact that an open position may exist.

## Boundary

ChatGPT chooses the opportunity. APEX validates the signal contract and performs the requested execution. The existing APEX intelligence stack remains available for its normal operating mode but is not used to reinterpret ChatGPT-originated signals.

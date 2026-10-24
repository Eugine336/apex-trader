# ChatGPT signal relay — paper-only

This optional integration accepts externally supplied ChatGPT opportunity candidates and records **simulated** Binance Futures fills. It is not a separate Apex trading authority.

## Execution boundary

- **PAPER:** supported. The paper executor uses public market prices and has no private order-placement capability.
- **LIVE:** blocked in code. Setting an environment variable or supplying API credentials does not override this block.
- Before any future LIVE support, the candidate must be converted to a first-class Apex opportunity, evaluated by `CognitiveBrain`, compared with the whole book, and admitted by the common execution/risk path. External signal text alone is not enough.

The low-level Binance order method, the live adapter, the shared signal executor, the relay endpoint, and the runtime readiness check all fail closed for LIVE. The relay health endpoint reports the effective mode as `PAPER_ONLY`.

## Start

Install the integration requirements and set a strong relay token in the runtime environment (not in source control):

```powershell
python -m pip install -r integrations/chatgpt_signals/requirements.txt
$env:CHATGPT_RELAY_TOKEN = '<long-random-secret>'
$env:BINANCE_EXECUTION_MODE = 'PAPER'
python -m integrations.chatgpt_signals.runtime_health
python -m integrations.chatgpt_signals.relay_server
```

Paper readiness expects:

```text
Execution mode       : PAPER
Live execution       : BLOCKED until CognitiveBrain admission is wired
Binance account      : NOT REQUIRED (paper mode)
Status               : READY FOR PAPER
```

A paper signal must use `mode: "PAPER"`, a positive quantity, a supported market-order type, and a timezone-aware expiration. The paper executor enforces the configured notional ceiling and prevents duplicate signal identifiers unless the explicit paper duplicate-testing option is enabled.

## Operational limitation

This integration does not prove a trading edge and must not be used to open live positions. Its current role is paper experimentation and intake testing only. Do not try to override the source guard through configuration.
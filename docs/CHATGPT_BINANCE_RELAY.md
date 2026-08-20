# ChatGPT -> APEX -> Binance Paper Relay

## Purpose

This path treats the ChatGPT opportunity-harvesting session as the trade-decision source and APEX as the execution engine. APEX does not reinterpret the ChatGPT thesis in this path.

The current implementation is PAPER-only. It uses Binance public Futures pricing for simulated fills and contains no Binance private trading credentials or live order endpoint.

## Data/control flow

```text
ChatGPT opportunity engine
        |
        | structured signal committed to chatgpt-signal-inbox
        v
GitHub Actions relay
        |
        | HTTPS POST + shared secret
        v
APEX relay_server.py
        |
        v
Binance paper executor
```

## Local startup

From the repository root:

```powershell
python -m pip install -r integrations/chatgpt_signals/requirements.txt
$env:CHATGPT_RELAY_TOKEN = '<long-random-secret>'
python -m integrations.chatgpt_signals.relay_server
```

The server listens on `127.0.0.1:8790` by default.

## Remote relay

GitHub Actions needs a reachable HTTPS endpoint. Recommended for a local/VPS demo is a private Cloudflare Tunnel or another authenticated tunnel terminating at `http://127.0.0.1:8790`.

Set these GitHub repository secrets:

- `APEX_SIGNAL_RELAY_URL` = the HTTPS tunnel URL, without `/v1/signals`
- `APEX_SIGNAL_RELAY_TOKEN` = exactly the same random value as `CHATGPT_RELAY_TOKEN`

Do not commit either value.

## Signal publication

A ChatGPT-originated signal is a JSON file on the `chatgpt-signal-inbox` branch under:

`signals/inbox/<signal_id>.json`

A push triggers the GitHub Action, which POSTs the signal to APEX. APEX validates the schema, expiry, mode, and duplicate state before producing a paper fill.

## Latency

This removes the 3-second local Git polling loop and replaces it with push-triggered GitHub Actions plus HTTPS delivery. It is substantially better for event-driven simulation, but GitHub Actions is still not a guaranteed sub-second trading transport. It must not be represented as tick-to-order latency.

## Safety

- PAPER mode only.
- No Binance API key required.
- No live order endpoint.
- Shared-secret authentication on the relay.
- Expired and duplicate signals are rejected.
- Keep the tunnel private/authenticated.

# ChatGPT → APEX Binance Paper Executor

This integration gives the ChatGPT opportunity-harvesting session a narrow execution mailbox inside APEX.

## Responsibility boundary

ChatGPT is the decision source. APEX does not reinterpret the trade thesis in this path.
It only validates the signal contract, rejects stale/duplicate/non-PAPER signals, and
creates a paper fill using the current Binance Futures public bid/ask.

The existing APEX intelligence stack remains intact for normal APEX operation.

## Transport

The initial transport is a dedicated Git branch named `chatgpt-signal-inbox`.
The executor clones/polls that branch every few seconds. ChatGPT can publish a JSON
signal to `signals/inbox/<signal_id>.json` on that branch.

This is intentionally simple and auditable. It is not a sub-second transport.
It is therefore suitable for simulation/integration testing, not production news
scalping.

## Run

From the repository root:

```powershell
python -m integrations.chatgpt_signals.inbox_runner
```

Environment variables:

```text
CHATGPT_SIGNAL_BRANCH=chatgpt-signal-inbox
CHATGPT_SIGNAL_POLL_SECONDS=3
CHATGPT_SIGNAL_WORKTREE=.runtime/chatgpt-signal-inbox
```

## Safety

The initial adapter is PAPER-only and uses only Binance public Futures endpoints.
It contains no Binance API-key authentication and no private order endpoint.
No live order can be placed by this adapter.

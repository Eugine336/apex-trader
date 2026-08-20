# ChatGPT Signal Inbox

This branch is a mailbox for structured trade instructions emitted by the ChatGPT opportunity-harvesting session.

The local APEX process should poll this branch and consume only files under `signals/inbox/`.

Signal files are JSON. A signal is valid only when:
- `mode` is `PAPER`
- `source` is `CHATGPT_OPPORTUNITY_ENGINE`
- `expires_at` is in the future
- `signal_id` has not been consumed before
- symbol/side/order type/quantity validate

Do not place live orders from this mailbox. The first implementation is simulation-only.

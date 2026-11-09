# MT5 broker-server clock bridge

## Why this exists

The runtime log showed MT5 tick epochs approximately 7,200 seconds ahead of the host UTC clock across many symbols. This is consistent with a broker server running at UTC+2, not dozens of bad instrument timestamps.

MetaQuotes' Python integration pages contain a UTC statement, but recent MQL5 moderator discussions explicitly clarify that returned MT5 tick/bar data use broker-server time and acknowledge the documentation wording is misleading. JustMarkets publishes MT5 market schedules in GMT+2/GMT+3. Apex must therefore use a live, broker-specific offset rather than hard-code +2 or +3.

References:
- [MQL5 moderator clarification: returned data use broker server time](https://www.mql5.com/en/forum/516958)
- [MQL5 moderator thread: approximately 2-hour offset on MetaQuotes-Demo](https://www.mql5.com/en/forum/519552)
- [JustMarkets market hours: schedules use server time GMT+3](https://justmarkets.com/markets/commodities)
- [MQL5 TimeTradeServer reference](https://www.mql5.com/en/docs/dateandtime/timetradeserver)
- [MQL5 TimeGMT reference](https://www.mql5.com/en/docs/dateandtime/timegmt)

## Install the publisher

1. Open MetaEditor from the exact MT5 terminal Apex connects to.
2. Open scripts/mt5_clock_publisher.mq5 from this repository.
3. Compile it as an Expert Advisor.
4. In that same terminal, attach APEX MT5 Server Clock Publisher to one chart. This EA contains no order functions and does not place, modify, or cancel orders.
5. Keep it running. It writes MQL5/Files/apex_mt5_clock_offset.txt every five seconds.
6. If Apex uses multiple independent MT5 terminals/accounts, attach the publisher to each terminal. The file is terminal-local and the Python reader checks the exact broker server name.

## Runtime behavior

- Python reads the publisher file under the connected terminal's data_path.
- The reader requires schema version 1, exact account server match, plausible offset, internally consistent server/GMT epochs, and a fresh file (20-second maximum).
- MT5 tick freshness is calculated in broker-server time using the publisher's TimeTradeServer()-TimeGMT() offset.
- TickData.time is normalized to UTC before it enters Apex cognition. Raw broker time, offset, and normalized epoch are preserved in raw_fields.
- Market and pending order preflights also require a fresh valid publisher sample. If the bridge is missing, stale, or belongs to another server, MT5 quotes are rejected and order entry fails closed.
- No fixed GMT+2/GMT+3 value is stored in .env; daylight-saving changes are read from the connected terminal.

## Diagnostic

```powershell
python scripts/diagnose_mt5_clock.py --symbols XAUUSD.ecn EURUSD.ecn
```

The Python diagnostic reports host clock state, MT5 server/company/build, and raw tick seconds/milliseconds. The EA publishes the live broker-server offset consumed by Apex.

## Scope limitation

This bridge normalizes live tick timestamps and validates the tick-time basis used by market/pending order preflights. Historical OHLCV, historical tick, deal, and order-history timestamps require a historical broker-offset series to normalize across daylight-saving transitions. They must not be relabelled as UTC using today's offset. That historical mapping remains a separate data-quality gate.

# Apex MT5 Python data capability contract

This document separates what the official MetaTrader 5 Python integration can
return from what Apex has actually wired into live reasoning. A callable API
method is not proof that the connected broker supplies the field for every
instrument.

## Native MT5 Python surfaces

| Surface | MT5 API | Apex status in this redesign |
|---|---|---|
| Terminal health and version | `terminal_info()`, `version()`, `last_error()` | Existing connection health; full snapshot exposed |
| Account state | `account_info()` | Existing normalized risk view; full broker snapshot exposed |
| Broker instrument catalogue | `symbols_total()`, `symbols_get()` | Native catalogue discovery added; arbitrary-symbol trading is not yet enabled |
| Instrument specification | `symbol_info()` | Existing broker spec plus full native fields |
| Latest quote | `symbol_info_tick()` | Existing live quote path |
| Historical bars | `copy_rates_from()`, `copy_rates_from_pos()`, `copy_rates_range()` | Existing multi-timeframe path; preserve tick volume, real volume and spread |
| Historical ticks | `copy_ticks_from()`, `copy_ticks_range()` | Range fetch added; replay ingestion and Brain attribution remain to be completed |
| Market depth | `market_book_add()`, `market_book_get()`, `market_book_release()` | Existing best-effort snapshot path; preserve raw and real volume distinctions |
| Active orders | `orders_total()`, `orders_get()` | Existing order/position reconciliation path |
| Open positions | `positions_total()`, `positions_get()` | Existing broker-book path |
| Order history | `history_orders_total()`, `history_orders_get()` | History retrieval is available; complete normalized ingestion remains |
| Deal history | `history_deals_total()`, `history_deals_get()` | Existing deal ingestion; preserve all broker fields |
| Broker calculations | `order_calc_margin()`, `order_calc_profit()`, `order_check()` | Must become the authoritative dynamic-instrument economics path |
| Execution | `order_send()` | Existing executor-only mutation boundary; not used during this redesign |

## Data semantics Apex must preserve

- MT5 bar `tick_volume`, `real_volume` and `spread` are separate fields. The
  legacy `volume` alias means tick volume only; it must never be described as
  exchange-traded volume.
- Tick `flags` are preserved. Aggressor side is populated only when the broker
  actually supplies the corresponding BUY/SELL flag; otherwise it remains unknown.
- Market-depth volume is not trade aggressor volume. Keep `volume` and
  `volume_real` distinct, and record which one was used in a derived measurement.
- Symbol specifications are broker/account specific and may change. Cache them
  with observation time and refresh policy; do not replace missing values with
  static registry estimates for a new instrument.
- Broker trade-session offsets are server-clock data. Without a verified server
  timezone/DST mapping and holiday calendar they are not UTC session truth.
- Every observation needs source, symbol, timestamp, freshness, and availability
  status. Empty/unavailable is not zero and is not evidence of absence.

## Not provided as a universal MT5 Python feed

The Python integration does not itself guarantee:
- a complete economic calendar with actual/forecast/previous releases;
- central-bank policy paths, yield curves, inflation series or macro releases;
- exchange-wide crypto funding, open interest or liquidation streams;
- true aggressor-side volume at every traded price, footprint data or centralized
  volume-at-price;
- complete depth-of-market for every broker and symbol;
- a verified server timezone, DST rule set or full holiday calendar.

Apex must use a separately configured provider for these sources or mark the
corresponding concept as data-gated. It must not infer the missing data from
OHLC or relabel broker tick volume as real traded volume.

## Production rule

The owner chooses the desired trading universe. The broker catalogue reports
what exists; instrument capability checks report what can be measured; the
Brain decides what is analytically relevant; the execution and owner-risk layers
decide whether a specific action is permitted. These are separate decisions.


## Critical timestamp-semantics reconciliation — WP-T (2026-11-11)

**Do not assume MT5 Python tick epochs are broker-local wall time.** The official MetaQuotes Python integration documentation for `copy_ticks_range` and `copy_ticks_from` explicitly states that MT5 stores tick and bar open times in UTC and that returned data has UTC time:

- https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksrange_py
- https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksfrom_py

The current Apex implementation has a competing assumption: it estimates a broker offset from the tick epoch and subtracts that offset from `time` and `time_msc` in `MT5Connector.get_ticks`; `get_price` likewise normalizes the latest tick by subtracting the estimated offset. The live diagnostic has previously reported an approximately +2-hour offset. These observations and the official API documentation are not yet reconciled by a captured raw terminal sample with an independent UTC witness.

**Required resolution before any normalization rewrite:**
1. Capture raw `time`, `time_msc`, terminal/account/server identity, independent UTC witnesses and monotonic receipt instants from the actual supported MT5 terminal without modifying its clock.
2. Repeat across multiple hosts with different local wall-clock settings and across broker server/DST transitions where possible.
3. Verify request-bound semantics for `copy_ticks_range` against the official UTC contract and compare returned raw epochs to the exact requested UTC window.
4. Add contract tests proving whether the live `symbol_info_tick` and historical `copy_ticks_range` surfaces share the documented UTC semantics for the supported terminal/package versions.
5. Until resolved, do not label an offset-adjusted timestamp as verified UTC merely because an offset estimate exists. Keep source semantics explicit and fail closed for consumers requiring normalized source time.

**Potential production consequence requiring proof:** if returned historical tick epochs are already UTC, subtracting a non-zero broker offset shifts them outside the requested UTC range. The poller then rejects rows outside its query window and routes only the latest quote fallback. This is a concrete test hypothesis, not yet an owner-host-confirmed runtime finding.

Do not silently treat broker chart display timezone, MQL5 `TimeTradeServer()`, MT5 Python `time`/`time_msc`, and UTC epoch as interchangeable.


### Evidence qualification update — 2026-11-11

The timestamp convention is **not conclusively settled for Apex's supported terminal/broker combinations**. MetaQuotes' official Python documentation for tick history says returned data have UTC time and requires UTC-aware request datetimes. The official `symbol_info_tick` reference lists raw `time` and `time_msc` fields but does not itself state their timezone semantics in the field description. MQL5 forum discussions contain conflicting user interpretations: some repeat the documented UTC interpretation; others report raw values matching broker-server civil time and disagree with the documentation. These reports are not authoritative specifications, but they are sufficient to reject an untested blanket assumption.

Evidence:
- Official `symbol_info_tick` reference: https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfotick_py
- Official `copy_ticks_range` reference: https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksrange_py
- Official `copy_ticks_from` reference: https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksfrom_py
- Conflicting field reports: https://www.mql5.com/en/forum/517344 and https://www.mql5.com/en/forum/519808

Do not convert this ambiguity into a hardcoded UTC or broker-server rule. Keep raw integer fields unchanged and distinguish API request-bound semantics from returned snapshot/history field semantics. The new read-only `scripts/diagnose_mt5_timestamp_semantics.py` captures raw live and historical samples, API/package/terminal version, server identity, host wall/monotonic readings, independent UTC witnesses and exact UTC request bounds. Run it on each supported host and preserve outputs. A source contract change requires correlated samples across hosts and versions, with no unexplained offset residual or DST discontinuity.

Until evidence resolves the contract, any timestamp conversion requiring the disputed interpretation must remain explicitly gated; do not silently apply the estimated broker offset twice or treat a wall-clock comparison as proof.


## Verified API-specific timestamp and completion contracts — 2026-11-11

MetaQuotes' official Python API reference states that returned tick and bar data are UTC. The `copy_rates_from_pos` reference separately defines position zero as the current bar; the preceding position is therefore the last completed bar. See the official references:
- `copy_ticks_range`: https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksrange_py
- `copy_rates_from`: https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrom_py
- `copy_rates_from_pos`: https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py

Owner-host captures on JustMarkets-Demo / XAUUSD.ecn repeatably show the live `symbol_info_tick` timestamp approximately UTC+2 while `copy_ticks_range` rows align with the UTC request bounds. This is a host/broker/package observation, not a universal claim about every terminal. The implementation now keeps the API contracts separate:

- Live `symbol_info_tick`: identity-bound broker-clock normalization remains active; raw provider epochs and the broker identity are preserved.
- `copy_ticks_range`: no live broker offset is subtracted. Only bounded (≤300-second) batches whose raw seconds and milliseconds are mutually consistent and inside the exact UTC request bounds are attested UTC. Broad or contradictory history remains timezone-unknown.
- `copy_rates_from_pos`: `start_pos=0` includes the forming bar; `start_pos=1` returns completed bars only. Returned bar epochs are UTC and retain raw source epochs.
- `copy_rates_range`: returned bar epochs are UTC; range membership is validated. Closed-only reads compare bar close times against a conservative independent UTC lower bound, not the broker-server offset.

Production confirmed-analysis and live-candle warmup fetchers now request `include_forming=False` and require `completed_bars_verified=True`. Cross-asset context likewise rejects any bar frame without completed-bar attestation. Same-host captures are repeated; second-host and DST-transition validation remain open.

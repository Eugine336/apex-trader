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

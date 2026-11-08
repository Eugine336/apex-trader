# Apex data-source audit and integration sequence

**Audit baseline:** `main` at `24b0ea4ae315e7e706f8bd461e95e00838796c6b`  
**Scope:** repository-visible code and contracts only. No broker credentials, `.env`, private account data, or live trading host were inspected. No provider subscription or broker entitlement is assumed.

## 1. Executive result

Apex already has meaningful broker-native data paths. The largest immediate gaps are not a lack of all market data; they are (a) multi-symbol synchronized bars are not passed into one concept-analysis call, (b) several existing broker observations are not yet fully attributed/replayed into the Brain, and (c) external calendar, macro, exchange-trade and derivatives feeds are not implemented as verified provider channels.

Do not buy external feeds before checking the connected broker's actual entitlements and the enabled instrument universe. Do not promote a gate merely because an API endpoint exists.

## 2. Existing source paths versus remaining gaps

| Channel | Repository evidence / current state | Remaining acceptance gap |
|---|---|---|
| MT5 completed OHLC bars | `MT5Connector.get_ohlcv` / `get_ohlcv_range`; broker source symbol and timestamp attestation; tick volume, real volume and spread are preserved separately. `SystemContext` fetches multi-timeframe frames for the target symbol. | Cross-symbol batch and common-cutoff delivery into the same concept reader. Verify actual history depth, gaps, symbol mapping and timestamp semantics on the connected broker. |
| MT5 historical bid/ask ticks | `MT5Connector.get_ticks` uses `copy_ticks_range`, preserves raw flags and only labels aggressor side when broker BUY/SELL flags exist. `PlatformManager.get_tick_history` and the SystemContext tick-history context are wired. | Complete replay/Brain attribution and production proof of source-time coverage. BUY/SELL tick flags are not a guarantee of exchange trade prints or true aggressor-side volume. |
| MT5 DOM | `get_market_depth` uses `market_book_add/get/release`; SystemContext supplies a depth snapshot to concept context. | Broker/symbol entitlement and repeated timestamped snapshots must be measured. DOM displayed volume is not executed trade volume; replenishment/depletion needs a time series. |
| MT5 instrument specifications | `symbol_info` path and instrument-economics reader exist; broker fields include tick/contract/volume constraints and swap long/short. | Verify freshness and completeness per enabled symbol. Carry still needs correct swap mode, rollover multipliers and account-currency conversion; non-MT5 funding/borrow is absent. |
| MT5 positions, orders, deals, equity | Connector exposes account and broker book/history surfaces; execution lifecycle reconciles broker-confirmed orders/deals. | Owner-connected complete whole-book and historical account trace for portfolio risk, risk-of-ruin and production acceptance. |
| Execution quote/fill outcomes | Execution monitor and quote-provenance path exist; PR #838 work is recorded in the implementation plan. | Attributable complete sample across intended size, side, contemporaneous quote, fill, commission, slippage and timestamps; owner-supplied broker history and independent source-manifest authentication. |
| Multi-symbol bars | `PlatformManager.fetch_all_market_data` supplies the configured universe. The current branch prepares one shared batch at `CognitionLoop.run_once` start, passes symbol-keyed frames, broker-attested contract metadata and an immutable cycle cutoff through `SystemContext`, and clears the snapshot at cycle end. `IntermarketReadings` computes exact-timestamp paired returns/correlation, currency contributions from verified base/profit metadata, three-bar-fractal SMT sweep comparisons and descriptive OLS residuals. | Reader implementation and initial tests are present. Production acceptance remains open for cross-broker fixtures, peer timestamp/cutoff edge cases, longer histories, relationship stability and formal residual stationarity; these methods remain DATA-GATED until acceptance. |
| Session calendar | MT5 can expose per-symbol weekly sessions, but connector documentation explicitly treats these as broker-server clock values. `session_calendar` requires timezone, DST, holidays, rollover and verified open/close. | Authoritative venue/broker timezone mapping, DST transitions, holidays/special sessions and rollover schedule. Weekly server sessions alone are insufficient. |
| Economic calendar | External gate reader explicitly requires trusted event time, actual, forecast, previous, relevance and provenance. No provider-backed calendar adapter was found in the audited paths. | Provider integration plus original-source IDs, publication/revision timestamps, actual/forecast/previous values and currency/instrument relevance. |
| Macro rates and growth | No provider-backed rates/yields/inflation series or complete release-history adapter was found in the audited paths. | Policy-rate history, nominal and real yields, yield curves, inflation series, GDP/PCE/CPI/employment/PMI releases and observation/publication timestamps. |
| Exchange volume-at-price / true aggressor flow | MT5 tick volume, real volume and DOM are kept distinct. Existing contract expressly says MT5 does not universally provide trade aggressor volume, footprint or centralized volume-at-price. | Exchange trade prints with aggressor classification and volume-at-price, or keep exact concepts data-gated. DOM is only a labelled proxy for depth behaviour. |
| Derivatives / crypto | No general exchange derivatives feed was found in the audited paths. Deriv synthetic history/contract metadata must be treated as provider-specific, not generalized. | Per-venue expiry/roll, options IV/skew/OI; crypto funding/OI/liquidations/basis from the exact exchange and instrument. |
| Calibration and Bayesian outcomes | CalibrationProducer and decision/outcome quote-provenance path exist. | Independently authenticated manifests, producer-attributed forecasts, broker-confirmed outcome lineage, selection-complete holdout and walk-forward datasets. |
| Seasonality / historical analogues | Historical candle readers exist. | Sufficient verified multi-year history and point-in-time dataset lineage by instrument/venue; historical coverage cannot be assumed from a callable API. |

## 3. External provider shortlist (candidate sources, not approved dependencies)

| Dataset | Candidate primary source | Integration rule |
|---|---|---|
| Economic events | Trading Economics calendar API; retain the original statistical-agency / central-bank release reference | Provider event IDs, actual/forecast/previous, revisions, event timezone, publication timestamp and currency/instrument mapping. Independently verify high-impact releases against the issuing authority. |
| US rates, yields and macro series | FRED/ALFRED; US Treasury; Federal Reserve | Preserve observation date separately from release/publication date. Use vintage/as-of data for historical replay; do not leak later revisions into earlier decisions. |
| Non-US policy rates and inflation | Relevant national central bank and national statistics authority | Select by currency and instrument relevance; preserve the original issuing authority and release metadata. |
| Futures/options market data | CME Group market data / reference data for COMEX gold and other applicable CME products | Licensing and entitlement must be confirmed. Keep futures contracts separate from broker CFDs/spot symbols. |
| Crypto derivatives | Exact exchange's official REST/WebSocket API (for example Binance only if that is the selected venue) | Venue + instrument + funding interval + event timestamp are mandatory. Never merge venues as if they were one book. |
| Synthetic contracts | Deriv official active-symbol/contract metadata and tick-history interfaces | Provider-specific contract specification and source timestamps; no cross-provider generalization. |
| Exchange calendars | Official exchange calendar and the instrument's broker trading schedule | Explicit timezone, DST, holidays, special sessions and rollover. |

No external provider is selected as a hard dependency in this audit. Pricing, rate limits, redistribution rights and account entitlements must be verified before implementation.

## 4. Mapping to the gated methods

| Data dependency | Analytical methods |
|---|---|
| Synchronized multi-symbol completed bars + canonical base/quote metadata | `cross_asset`, `currency_strength`, `smt_divergence`, `relative_value` |
| Economic event records + macro time series | `event_driven`, `macro_growth`, `macro_rates` |
| Futures/options contract and market data | `derivatives` |
| Exchange funding/OI/liquidation/basis | `crypto_microstructure` |
| Provider-specific synthetic contract specification and history | `synthetic_market` |
| Executed trade prints + aggressor side + volume-at-price | `delta_cvd`, `footprint`, `absorption_exhaustion`, `volume_profile`, `vwap` |
| Attributable broker fills + contemporaneous quotes/costs | `market_impact` |
| Broker whole-book positions + equity + synchronized returns | `portfolio_risk` |
| Point-in-time concept forecasts and outcomes | `bayesian`, `calibration` |
| Deduplicated broker-confirmed closed-trade R history | `monte_carlo` |
| Authoritative timezone/DST/holiday/session schedule | `session_calendar`, `opening_range` |
| Direction-specific financing, rollover and conversion | `carry` |

The gated lenses consume these same channels; they do not justify separate duplicate feeds. Their mapping remains in `cognition/concepts/strategy_lens_readings.py` and the generated coverage register.

## 5. Implementation sequence

### D0 — Broker capability discovery (first; read-only)
A new read-only runner is available at `scripts/audit_broker_data_capabilities.py`. It probes the connected MT5 adapters and emits aggregate account/order/deal metadata plus per-symbol OHLC, tick, DOM, specification and broker-session availability. It deliberately omits login IDs and raw position/order/deal rows, does not read `.env`, and performs zero execution mutations. Run it on the owner-connected Windows trading host; repository CI cannot prove broker entitlements or historical coverage.

1. On the owner-connected host, capture MT5 terminal/broker identity, enabled symbol catalogue and per-symbol specification snapshots.
2. For each enabled symbol/timeframe, measure completed-bar history depth, gaps, duplicate/out-of-order timestamps and source-time attestation.
3. For each enabled symbol, measure tick history availability, bid/ask coverage, raw flags and whether BUY/SELL flags are actually present.
4. Probe DOM availability and record depth level count, volume basis and observation timestamps; unavailable DOM stays unknown.
5. Capture broker session schedules without converting server time to UTC unless timezone/DST mapping is independently verified.
6. Reconcile positions, orders, deals, account equity and historical fills read-only.

**Output:** a per-symbol capability matrix with AVAILABLE / PARTIAL / UNAVAILABLE / UNVERIFIED, sample counts, observed date range, timestamp quality and provider errors. Do not submit, modify or cancel orders.

### D1 — Multi-symbol completed-bar channel
1. **Implemented on the current branch:** reuse `fetch_all_market_data` at the beginning of one cognition cycle and share the returned per-symbol frames through the concept context.
2. **Implemented on the current branch:** capture one immutable UTC cycle cutoff and release the snapshot after the cycle.
3. **Implemented on the current branch:** preserve the original per-symbol DataFrames and their broker-attested source metadata; retain the existing single-symbol fallback when batch data is unavailable.
4. **Implemented on the current branch:** `IntermarketReadings` validates peer identity through adapter provenance, excludes forming/future bars at the cycle cutoff, aligns exact source timestamps, and emits non-directional cross-asset, currency-strength, SMT and relative-value measurements. Currency decomposition requires verified adapter-supplied base/profit metadata; it never parses symbol names.
5. **Implemented on the current branch:** initial tests cover synchronized return measurements, source-symbol mismatch rejection, forming/future-bar exclusion and suppression of contradictory generic data-gate evidence.
6. **Remaining acceptance:** broaden provider-neutral fixtures across MT5/Deriv/other adapters, test missing/duplicate/asynchronous/stale timestamps and DST/timeframe boundaries, validate currency metadata for all enabled symbols, add multi-period robustness and formal stationarity tests, and prove evidence lineage through Brain replay. These methods remain DATA-GATED until acceptance.

### D2 — Session and financing completion
1. Normalize broker server session schedules only with an independently verified timezone/DST mapping.
2. Add official holidays and special sessions; preserve unknown when no authoritative calendar exists.
3. Complete direction-specific swap mode, rollover multiplier, accrual interval and account-currency conversion.
4. Validate against broker statements/history; do not infer cash financing from nominal swap points without conversion.

### D3 — Calendar and macro provider adapter
1. Select provider after terms, entitlement, rate limits and required fields are confirmed.
2. Normalize event identity, event time, publication time, revision time, actual, forecast, previous, currency, instrument relevance and source reference.
3. Preserve vintages so replay only sees information available at its historical cutoff.
4. Verify a sample against issuing authorities and test missing, revised, delayed and conflicting releases.

### D4 — Exchange microstructure and derivatives
1. Determine whether the enabled instrument universe requires COMEX/futures or crypto exchange data.
2. Obtain licensed trade prints/volume-at-price and aggressor classification where needed.
3. Keep DOM, tick volume, real volume, exchange trade volume and inferred aggressor estimates as separate bases.
4. Add per-venue derivatives metadata and feeds only for instruments actually enabled.

### D5 — Learning, portfolio and production acceptance
1. Complete owner-connected broker history and whole-book snapshots.
2. Authenticate decision/outcome manifests and maintain development, untouched holdout and walk-forward partitions.
3. Verify complete quote/fill/cost attribution and closed-trade R outcomes.
4. Run the full-universe benchmark on the owner-connected host.
5. Promote a concept only after real-data replay, point-in-time/no-look-ahead, provenance, completeness, Brain integration and fault-degradation acceptance.

## 6. Mandatory normalized observation contract

The current branch adds `data_sources/contracts.py` and `data_sources/registry.py` as vendor-neutral, read-only interfaces. They define provider/venue and canonical/native instrument identity, observation kind, source/receipt/publication/revision timestamps, verification state, completeness, units, volume/aggressor basis and capability reports. `DataSourceRegistry` validates provider identity, observation kind and requested instrument/time window. It has no vendor SDK, credentials, network client or execution methods; concrete provider adapters are a later integration workstream.

Every provider observation must retain:
- provider and venue;
- canonical instrument identity and provider-native symbol;
- observation type and units;
- source event/observation timestamp;
- received timestamp;
- publication and revision timestamps when applicable;
- source record/event/trade reference;
- source timestamp verification and its basis;
- completeness and freshness state;
- volume/aggressor basis where applicable;
- unknowns and provider errors.

A provider response being syntactically valid is not timestamp verification. Receipt time must never be substituted for source time. A missing value is not zero. Revised macro data must not overwrite the vintage available at an earlier decision cutoff.

## 7. Safety and scope constraints

- Read-only data discovery only. No broker orders, modifications, cancellations or live trading.
- No changes to `.env`, secrets, account configuration or deployed owner limits.
- No fabricated credentials, subscriptions, entitlements, timestamps, trade prints or historical observations.
- No weakening of RiskGate, ActionExecutor, broker feasibility or owner controls to make a data channel appear available.
- Do not relabel quote-tick estimates as executed aggressor flow or MT5 tick volume as exchange volume.
- Missing feeds remain explicitly unknown and data-gated.

## 8. Acceptance definition

A data-gated method can be promoted only when all its named fields are present from an approved source, instrument and timestamp lineage are verified, history is sufficiently complete for the reader, point-in-time replay is clean, faults degrade visibly to unknown, the reader is wired into the Brain, and tests use real provider samples or independently captured fixtures. A coverage-register edit alone is not acceptance.


## 10. Current implementation increment — normalized provider readers

The active implementation branch now contains a provider-neutral `DataSourceRegistry` and native Brain readers for normalized observations. The registry supports independent provider failure isolation through `fetch_with_status`: healthy provider observations survive an outage or malformed response from another adapter, while provider-specific exception classes are surfaced in evidence facts. Its strict `fetch` compatibility method raises if any selected provider fails.

Implemented normalized observation consumers:
- Economic calendar events preserve event time separately from publication/revision vintages. Future scheduled event times are allowed only when the record was already published by the analysis cutoff.
- Macro series require verified instrument currency metadata, observation time and publication time; revisions after the cutoff are excluded.
- Derivatives and crypto funding/open-interest/liquidation observations retain provider, venue, native instrument and source record identity.
- Delta/CVD and footprint require exchange-classified aggressor side and executed-trade volume. Tick-volume proxies and inferred aggressor sides are rejected.
- Market impact uses broker-confirmed fills paired with contemporaneous executable bid/ask and fill timestamps. It does not emit predictive fill probability.
- Session calendar can consume normalized timezone-aware, source-timestamped session observations. Unknown DST, holiday, rollover or tradeability fields remain explicit.

These are reader implementations and contract-level tests, not evidence that any real provider is connected or licensed. The registry is currently initialized without vendor adapters. Actual provider selection, credentials, entitlement checks, historical backfill, rate limits, outage handling and owner-connected validation remain a separate integration gate. Do not mark these concepts FULL solely because a reader exists.

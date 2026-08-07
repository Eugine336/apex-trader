# APEX TRADER — Full Data-Path Trace (READ-ONLY ANALYSIS)

**Scope:** Trace every field of `EntryContext` and `TradeContext` from the
WorldModel (single source of truth) through the analysis/extraction helpers into
the two decision planes (entry adjudication + in-trade management), for both the
LIVE path (`event_driven_bootstrap.py`) and the BACKTEST path
(`brain/backtest_engine.py`), and record which consumer actually READS each field.

**Method:** Raw code only. Every claim carries a `file:line` reference. Comments
and docstrings were ignored except where explicitly quoted as "the code says".

**Commit context:** This trace reflects the repository as read at the current
HEAD. Several fixes discussed in prior sessions (entry-side HTF events,
counter-trend conviction penalty, alignment gate) are **NOT present** in the code
on disk — they are documented here as live gaps, not as fixed.

---

## Section 1 — WorldModel Data Inventory

The `WorldModel` is a frozen, per-symbol snapshot
([brain/world_model.py:34-97](brain/world_model.py)). Everything the decision
planes know is read from it (plus broker tick truth + session/news engines).

| WorldModel field / accessor | file:line | Represents | Timeframes |
|---|---|---|---|
| `fvgs` / `fvgs_by_tf()` | world_model.py:51, 101 | Fair-value gaps per TF | M5/M15/H1/H4 (per TF_MODULE_MAP) |
| `order_blocks` / `order_blocks_by_tf()` | world_model.py:52, 104 | Order blocks per TF | M5/H1/H4 |
| `structure` / `structure_by_tf()` | world_model.py:53, 107 | `StructureAnalysis` per TF — `.trend`, `.confidence`, `.last_event` (BOS/CHOCH), swing points | M5/M15/H1/H4/D1 |
| `liquidity` / `liquidity_by_tf()` | world_model.py:54, 110 | Liquidity map | H1/H4 |
| `volume` / `volume_by_tf()` | world_model.py:55, 113 | Volume analysis | M5/H1 |
| `wyckoff` / `wyckoff_by_tf()` | world_model.py:56, 116 | Wyckoff phase | H1 |
| `inducement` / `inducement_by_tf()` | world_model.py:57, 119 | Inducement / reversal | varies |
| `bias` / `bias_dict()` | world_model.py:61, 122 | Combined HTF bias dict — `direction`, `score`, `strength`, `confidence` | D1+H4+H1 fused |
| `entry_zones` / `entry_zones_list()` | world_model.py:68, 125 | Synthesized `EntryZone` list (conviction, direction, `is_counter_trend`, `bias_direction`) | producer TF |
| `concepts` / `concepts_by_tf()` | world_model.py:76, 129 | Non-ICT concept signals | per TF |
| `regime` / `regime_by_tf()` | world_model.py:77, 133 | Volatility-regime label string | per TF |
| `votes` / `votes_list()` | world_model.py:85, 137 | Per-module directional `Vote` panel | synthesized |
| `candidates` / `candidates_list()` | world_model.py:86, 141 | Ranked `Opportunity` list | synthesized |
| `opportunity_quality` | world_model.py:94 | OQ score (0–10) or `None` | symbol-level |
| `entry_quality_long` / `entry_quality_short` | world_model.py:95-96 | EQ score (0–10) or `None` | per direction |
| `regime_analysis` | world_model.py:97 | `RegimeAnalysis` object or `None` | symbol-level |

### Extraction helpers (how data leaves the WorldModel)

| Helper | file:line | Returns | Used by |
|---|---|---|---|
| `_struct_trend_conf(struct, tf)` | event_driven_bootstrap.py:58-70 | `(trend_str, confidence)`; `("UNKNOWN", 0.0)` if TF absent | live entry + mgmt builds |
| `_struct_event(struct, tf)` | event_driven_bootstrap.py:73-88 | last BOS/CHOCH string; `"NONE"` if absent/none | live **mgmt only** (NOT live entry) |
| `_management_micro_context(symbol, dir)` | event_driven_bootstrap.py:709-787 | dict `m1_aligned_count / m1_event / m1_trend / h1_last_candle_bearish / h1_last_candle_doji` (live M1@100 + H1@200) | live mgmt build |
| `_resolve_bias(struct_by_tf)` | entry/zone_watcher.py:26-37 | first non-RANGING trend across H4→H1→D1 → `"LONG"/"SHORT"/None` | zone extraction (`is_counter_trend`) |
| `_struct_trend_conf` / `_struct_event` (backtest copies) | brain/backtest_engine.py (module level) | same contract | backtest entry + mgmt builds |
| `_micro_from_slice(M1_df, is_long)` | brain/backtest_engine.py:1426-1444 | m1 alignment/event/trend from M1 candle slice | backtest entry + mgmt builds |
| `_h1_candle_context(H1_df, is_long)` | brain/backtest_engine.py:1809 (helper) | h1 last-candle bearish/doji | backtest mgmt build |
| `_oq_eq_from_wm(wm, dir)` | brain/backtest_engine.py:1841 (helper) | `(live_oq, live_eq)` | backtest mgmt build |

---

## Section 2 — Entry Plane: Field-by-Field Trace (`EntryContext`)

`EntryContext` definition: [decision/context.py:141-242](decision/context.py).
Live builder: [event_driven_bootstrap.py:4775-4808](event_driven_bootstrap.py)
(inside `_on_entry_decision`, "Gate 6").
Backtest builder: [brain/backtest_engine.py:1288-1315](brain/backtest_engine.py)
(`_build_entry_context`).
Consumers: `SituationEngine.assess_entry` (situation.py:185-336),
`DecisionEngine.decide_entry` (engine.py:926-1222),
`DecisionEngine._decide_entry_action` (engine.py:1224-1250),
`RiskGovernor.review_entry` (governor.py:213-301), `DecisionJournal` (logging).

> **Entry-plane handoff note:** the live entry signal arrives as a `decision`
> dict built in [entry/entry_orchestrator.py:258-274](entry/entry_orchestrator.py).
> That dict carries ONLY: `symbol, direction, entry_price, stop_loss, tp1, tp2,
> conviction(=zone score), zone_type, timeframe, gates_passed, risk_pips,
> spread_pips, has_sweep, is_counter_trend, bias_direction`. It does **NOT**
> carry `m1_aligned` or `m1_event`, so the live builder's
> `decision.get("m1_aligned", 3)` / `decision.get("m1_event", "")`
> (bootstrap:4792-4793) **always fall through to the `3` / `""` defaults**.

```
Field: symbol
Type: str    Default: ""
Live source:   bootstrap:4776 — symbol
Backtest src:  backtest:1289 — pair
Consumer(s):   journal/governor logging, evidence strings
Status: USED (identity)
```
```
Field: direction
Type: str ("LONG"/"SHORT")    Default: ""
Live source:   bootstrap:4777 — normalized from order direction
Backtest src:  backtest:1290 — direction
Consumer(s):   is_long property → drives EVERY directional check in assess_entry
               (tf alignment, momentum events, structure events, consensus)
Status: USED (critical)
```
```
Field: scan_score
Type: int    Default: 0
Live source:   bootstrap:4778 — scan_score=conviction (= zone.conviction, 70/80/100)
Backtest src:  backtest:1291 — score
Consumer(s):   engine._decide_entry_action:1246 — smoothstep(scan_score,60,80)
               for MARKET-vs-PENDING preference
Status: USED
```
```
Field: scan_direction
Type: str    Default: ""
Live source:   NOT SET → "" (falls through)
Backtest src:  backtest:1292 — scan_direction=direction (own direction)
Consumer(s):   engine reads ctx.scan_direction ONLY in decide_management
               (engine.py:550) — never in decide_entry/assess_entry
Status: DEAD (EntryContext.scan_direction is never read on the entry plane;
        also MISMATCH: live="" vs backtest=direction, but harmless since unread)
```
```
Field: oq    (Opportunity Quality 0–10)
Type: float    Default: 0.0
Live source:   NOT SET → 0.0
Backtest src:  NOT SET → 0.0
Consumer(s):   none — grep finds no `ctx.oq` read in decision/ or anywhere
Status: DEAD (carried by the dataclass, never populated, never read)
```
```
Field: eq    (Entry Quality 0–10)
Type: float    Default: 0.0
Live source:   NOT SET → 0.0      Backtest src: NOT SET → 0.0
Consumer(s):   none (no `ctx.eq` read)
Status: DEAD
```
```
Field: scan_timestamp
Type: float    Default: 0.0
Live source:   NOT SET → 0.0      Backtest src: NOT SET → 0.0
Consumer(s):   none
Status: DEAD
```
```
Field: entry_type   (zone type, e.g. "FVG_OB_OVERLAP")
Type: str    Default: ""
Live source:   bootstrap:4779 — decision.get("zone_type","")
Backtest src:  backtest:1293 — zone.zone_type.value
Consumer(s):   assess_entry:237-251 — zone-quality bonus to structure_integrity
               (+0.30 overlap / +0.20 OB / +0.15 FVG / +0.25 sweep);
               engine._reversal_evidence:342 ("SWEEP" detection)
Status: USED (critical — drives entry structure score)
```
```
Field: entry_price / stop_loss / tp1 / tp2
Type: float    Default: 0.0
Live source:   bootstrap:4780-4783
Backtest src:  backtest:1294-1297
Consumer(s):   used to compute risk_reward_2 / risk_pips at build time; raw
               values not re-read by the engine (engine reads risk_reward_2)
Status: USED (inputs to derived fields)
```
```
Field: risk_reward_1
Type: float    Default: 0.0
Live source:   NOT SET → 0.0      Backtest src: NOT SET → 0.0
Consumer(s):   none (engine only reads risk_reward_2)
Status: DEAD
```
```
Field: risk_reward_2
Type: float    Default: 0.0
Live source:   bootstrap:4784 — abs(tp2-entry)/abs(entry-sl)
Backtest src:  backtest:1298 — same formula
Consumer(s):   decide_entry:979 (+ENTER bonus when >2.0), :1044 (SKIP when <1.5);
               governor.review_entry:268-270 (graded risk dimension)
Status: USED (critical)
```
```
Field: risk_pips
Type: float    Default: 0.0
Live source:   bootstrap:4785      Backtest src: backtest:1299
Consumer(s):   carried for sizing/journal; not read by assess_entry/decide_entry
Status: USED (downstream sizing/journal)
```
```
Field: entry_mode
Type: str    Default: "PENDING"
Live source:   NOT SET → "PENDING"      Backtest src: NOT SET → "PENDING"
Consumer(s):   engine._decide_entry_action:1234 — if "MARKET" → ENTER_MARKET
Status: USED but DEFAULT-THROUGH (always "PENDING" in both planes → the
        MARKET fast-path is unreachable from either context builder)
```
```
Field: micro_confirmation
Type: str    Default: ""
Live source:   NOT SET → ""      Backtest src: NOT SET → ""
Consumer(s):   engine._decide_entry_action:1243 (MARKET if choch_bos/engulfing/
               pin_bar); _reversal_evidence:343/374 ("sweep")
Status: USED but DEFAULT-THROUGH (always "" in both planes → micro-confirmation
        MARKET term and the sweep half of reversal evidence never fire)
```
```
Field: is_counter_trend
Type: bool    Default: False
Live source:   bootstrap:4794 — decision.get("is_counter_trend", False)
Backtest src:  backtest:1308 — zone.is_counter_trend
Consumer(s):   NONE in decision engine. engine._is_counter_htf:330-334 derives
               counter-trend independently from ctx.h4_trend, NOT this flag.
               Only other read is entry_orchestrator:272 (building the dict).
Status: DEAD as a decision input (populated end-to-end but the DecisionEngine
        never reads the flag; counter-trend handling keys off h4_trend instead)
```
```
Field: bias_direction
Type: str    Default: ""
Live source:   bootstrap:4795 — decision.get("bias_direction","")
Backtest src:  backtest:1309 — zone.bias_direction
Consumer(s):   none in decision/ (grep: only set sites)
Status: DEAD as a decision input
```
```
Field: base_lots / account_balance / risk_pct
Type: float    Default: 0.0 / 0.0 / 0.0
Live source:   NOT SET (base_lots, account_balance) → 0.0 ; risk_pct NOT SET → 0.0
Backtest src:  account_balance=backtest:1300, risk_pct=backtest:1301 (SET);
               base_lots NOT SET → 0.0
Consumer(s):   not read by assess_entry/decide_entry; sizing happens downstream
               via PositionSizer with its own inputs
Status: account_balance/risk_pct USED (backtest only, sizing); base_lots DEAD;
        MISMATCH: live leaves account_balance/risk_pct at 0.0
```
```
Field: d1_trend / d1_confidence
Type: str/float    Default: "UNKNOWN" / 0.0
Live source:   bootstrap:4786-4787 via _struct_trend_conf(structure,"D1")
Backtest src:  backtest:1302 via _struct_trend_conf
Consumer(s):   assess_entry:193-198 (tf_alignment, weight 0.40); read_confidence:306
Status: USED
```
```
Field: d1_event
Type: str    Default: "NONE"
Live source:   *** NOT SET *** → "NONE"  (live entry build omits it)
Backtest src:  backtest:1302 — _struct_event(structure,"D1")  (REAL value)
Consumer(s):   assess_entry:263-271 — d1_opposing → structure_integrity -0.25
Status: MISMATCH-WITH-BACKTEST + DEFAULT-THROUGH (live). On the LIVE entry plane
        d1_event is always "NONE", so the D1 structure-break penalty NEVER fires
        at entry; backtest feeds the real event. (Root of the entry-vs-management
        structure contradiction.)
```
```
Field: h4_trend / h4_confidence
Type: str/float    Default: "UNKNOWN"/0.0
Live source:   bootstrap:4788-4789      Backtest src: backtest:1303
Consumer(s):   assess_entry tf_alignment (weight 0.35); engine._is_counter_htf:333
               (reads h4_trend to decide reversal); read_confidence:308
Status: USED (critical — h4_trend is the counter-HTF determinant)
```
```
Field: h4_event
Type: str    Default: "NONE"
Live source:   *** NOT SET *** → "NONE"
Backtest src:  backtest:1303 — _struct_event(structure,"H4")  (REAL)
Consumer(s):   assess_entry:253-262 — h4_opposing → structure_integrity -0.30
Status: MISMATCH-WITH-BACKTEST + DEFAULT-THROUGH (live). Live entry NEVER sees
        an H4 break → entry structure_integrity stays high even when H4 has
        broken against the trade. **Primary entry-blindness gap.**
```
```
Field: h1_trend / h1_confidence
Type: str/float    Default: "UNKNOWN"/0.0
Live source:   bootstrap:4790-4791      Backtest src: backtest:1304
Consumer(s):   assess_entry tf_alignment (weight 0.25)
Status: USED
```
```
Field: h1_event
Type: str    Default: "NONE"
Live source:   *** NOT SET *** → "NONE"
Backtest src:  backtest:1304 — _struct_event(structure,"H1")  (REAL)
Consumer(s):   NONE on the entry plane — assess_entry checks only H4 and D1
               events (situation.py:253-271), never H1.
Status: DEAD on entry plane (even in backtest where it is populated). Note: H1
        events ARE read on the management plane (situation.py:553-566).
```
```
Field: m1_trend
Type: str    Default: "UNKNOWN"
Live source:   *** NOT SET *** → "UNKNOWN"
Backtest src:  backtest:1305 — micro["m1_trend"] (REAL from M1 slice)
Consumer(s):   assess_entry read_confidence:310 (+0.15 when != "UNKNOWN")
Status: MISMATCH + DEFAULT-THROUGH (live). Live entry loses the +0.15 confidence
        credit on every trade; backtest earns it.
```
```
Field: m1_event
Type: str    Default: "NONE"
Live source:   bootstrap:4793 — decision.get("m1_event","")  → *** always "" ***
               (the orchestrator decision dict never sets "m1_event")
Backtest src:  backtest:1306 — micro["m1_event"] (REAL)
Consumer(s):   assess_entry:215-223 — momentum event ±0.30/0.25
Status: MISMATCH + DEFAULT-THROUGH (live). Live entry momentum-event term never
        fires (value is "", not in any opposing/supporting set); backtest feeds
        the real M1 event.
```
```
Field: m1_aligned_count
Type: int (0-5)    Default: 0
Live source:   bootstrap:4792 — decision.get("m1_aligned", 3) → *** always 3 ***
Backtest src:  backtest:1307 — micro["m1_aligned_count"] (REAL 0-5)
Consumer(s):   assess_entry:212-214 — candle momentum = (count-2.5)/2.5 *0.50
Status: MISMATCH + DEFAULT-THROUGH (live). Live entry candle-momentum is pinned
        at (3-2.5)/2.5*0.50 = +0.10 for EVERY trade; backtest uses the real count.
```
```
Field: session_name
Type: str    Default: "UNKNOWN"
Live source:   NOT SET → "UNKNOWN"      Backtest src: NOT SET → "UNKNOWN"
Consumer(s):   not read by assess_entry (entry urgency reads session_tradeable)
Status: DEAD on entry plane
```
```
Field: session_tradeable
Type: bool    Default: True
Live source:   NOT SET → True      Backtest src: NOT SET → True
Consumer(s):   assess_entry:291-294 — urgency 0.6 when NOT tradeable
Status: USED but DEFAULT-THROUGH (always True in both planes → entry session-
        urgency never fires at the DE; the EntryGate covers session separately
        via is_session_active, gate.py:133)
```
```
Field: minutes_to_high_impact_news
Type: float    Default: 999.0
Live source:   NOT SET → 999.0      Backtest src: NOT SET → 999.0
Consumer(s):   assess_entry:287-290 — news urgency when <15min
Status: USED but DEFAULT-THROUGH (always 999 in both planes → entry news-urgency
        never fires at the DE; EntryGate covers news via is_news_clear, gate.py:150)
```
```
Field: news_impact
Type: str    Default: "NONE"
Live source:   NOT SET → "NONE"      Backtest src: NOT SET → "NONE"
Consumer(s):   assess_entry:290 — cosmetic (evidence string only)
Status: DEAD (cosmetic)
```
```
Field: open_trade_count / max_open_trades
Type: int    Default: 0 / 5
Live source:   bootstrap:4796-4797 — len(open_positions) / config.risk.max_open_trades
Backtest src:  backtest:1310-1311 — 0 / config.risk.max_open_trades
Consumer(s):   governor.review_entry:237 — hard veto when count >= max
Status: USED (live). Backtest always passes 0 (single-position serial replay) →
        the max-trades veto can never bind in backtest. MISMATCH (by design).
```
```
Field: portfolio_heat_pct
Type: float    Default: 0.0
Live source:   bootstrap:4798 — account_risk.heat(account_key)  (REAL)
Backtest src:  NOT SET → 0.0
Consumer(s):   governor.review_entry:259 (graded heat dim), :317 (legacy veto)
Status: USED (live). MISMATCH: backtest heat is always 0.0 → heat dimension /
        veto never engages in backtest.
```
```
Field: current_spread / typical_spread
Type: float    Default: 0.0 / 0.0
Live source:   bootstrap:4799-4800 — _get_spread_pips / registry typical (REAL)
Backtest src:  NOT SET → 0.0 / 0.0
Consumer(s):   governor.review_entry:262-267 (graded spread dim, GUARDED by
               typical_spread>0), :321-325 (legacy veto)
Status: USED (live). MISMATCH: backtest leaves both 0.0 → the spread dimension is
        skipped entirely (guard `typical_spread>0` false). journal:154-155 logs.
```
```
Field: regime
Type: str    Default: ""
Live source:   *** NOT SET *** → ""
Backtest src:  backtest:1312 — wm.regime_by_tf().get("H1","")  (REAL)
Consumer(s):   engine.decide_entry:935 — _weights_for_regime(ctx.regime)
Status: MISMATCH + DEFAULT-THROUGH (live). Live entry always uses the base
        DecisionWeights (regime=""); backtest selects regime-specific weights.
        Live and backtest can therefore weight the SAME setup differently.
```
```
Field: ev_estimate / pair_multiplier
Type: float    Default: 0.0 / 1.0
Live source:   NOT SET      Backtest src: NOT SET
Consumer(s):   journal:156-157 only (logging)
Status: DEAD (cosmetic / logging)
```
```
Field: horizon
Type: str    Default: ""
Live source:   *** NOT SET *** → ""
Backtest src:  NOT SET → ""
Consumer(s):   engine.decide_entry:940-946 — _horizon_htf_scale → HTF demotion
               for SCALP/SWING/MIXED ranker ideas
Status: USED but DEFAULT-THROUGH (both planes). Always "" → full HTF authority,
        HTF demotion never engages from either context builder.
```
```
Field: confluences
Type: list    Default: []
Live source:   NOT SET → []      Backtest src: backtest:1313 — signal.confluences
Consumer(s):   none in assess_entry/decide_entry; journal/trade_journal only
Status: DEAD as a decision input (cosmetic); MISMATCH live=[] vs backtest=real
```
```
Field: consensus_votes
Type: list[Vote]    Default: []
Live source:   bootstrap:4804-4807 — list(wm.votes)
Backtest src:  backtest:1314 — list(votes)
Consumer(s):   assess_entry._assess_consensus:338-401 → consensus_alignment +
               consensus_components; decide_entry:993-1073 (ENTER support, SKIP
               pressure, high-authority veto); compute_conviction:1266-1268
Status: USED (critical — full panel reaches both planes)
```

---

## Section 3 — Management Plane: Field-by-Field Trace (`TradeContext`)

`TradeContext` definition: [decision/context.py:12-138](decision/context.py).
Live builder: [event_driven_bootstrap.py:951-1000](event_driven_bootstrap.py)
(inside `_run_decision_engine_management`).
Backtest builder: [brain/backtest_engine.py:1897-1937](brain/backtest_engine.py)
(`_build_trade_context`).
Consumers: `SituationEngine.assess_open_trade` (situation.py:136-183 + helpers
435-674), `DecisionEngine.decide_management` (engine.py:467-…),
`RiskGovernor.review` (governor.py), `_set_protective_stop` (engine.py:888-906),
`DecisionJournal`.

```
Field: symbol / order_id / direction
Live source:   bootstrap:952-954      Backtest src: backtest:1898-1900
Consumer(s):   is_long property → all directional checks; fast_opp logging
Status: USED (critical)
```
```
Field: entry_type
Type: str    Default: ""
Live source:   *** NOT SET *** → ""
Backtest src:  backtest:1901 — trade.entry_type / setup.zone_type
Consumer(s):   is_adopted property (== "ORPHAN_ADOPTED") → decide_management:472
               adopted-observation branch & _derive_label:679
Status: DEFAULT-THROUGH (live). Live mgmt never sets entry_type → is_adopted
        always False → adopted-observation path is unreachable in live mgmt.
        MISMATCH: backtest sets it.
```
```
Field: entry_price / current_price / current_sl
Live source:   bootstrap:955-957      Backtest src: backtest:1902-1904
Consumer(s):   pnl/risk math; _set_protective_stop comparisons
Status: USED
```
```
Field: pnl_pips
Live source:   bootstrap:958 (computed from broker price)
Backtest src:  backtest:1905 (computed from candle close)
Consumer(s):   profit_r property → profit_state (situation.py:158) → CLOSE terms
Status: USED (critical)
```
```
Field: pnl_dollars
Type: float    Default: 0.0
Live source:   bootstrap:959 — _broker_pnl(pos)
Backtest src:  backtest:1906 — pnl_pips * pip_value * lots
Consumer(s):   thesis-secure path (engine, min_profit_usd gate)
Status: USED
```
```
Field: hold_minutes
Live source:   bootstrap:960 (now - entry_time)
Backtest src:  backtest:1907 (candle time - entry_time)
Consumer(s):   situation.maturity:168; decide_management adopted window:472
Status: USED
```
```
Field: at_breakeven / tp1_hit / trailing / partial_closed
Type: bool    Default: False
Live source:   bootstrap:961-964 (from mgmt state)
Backtest src:  backtest:1909-1911 (from trade dict); at_breakeven backtest:1908
Consumer(s):   governor.review / engine management action routing (TIGHTEN/SECURE)
Status: USED
```
```
Field: lots / original_risk_pips
Live source:   bootstrap:965-966      Backtest src: backtest:1912-1913
Consumer(s):   original_risk_pips → profit_r denominator (context.py:136-138)
Status: USED (critical — profit_r drives loss-response CLOSE)
```
```
Field: scan_score   *** KEY MISMATCH FIELD ***
Type: int    Default: 0
Live source:   bootstrap:861-866,967 — max conviction of zones whose direction
               EQUALS the trade's OWN direction (want_dir = trade dir)
Backtest src:  backtest:1866-1874,1914 — same: own-direction zone conviction
Consumer(s):   decide_management:550-560 — opposing-scan CLOSE boost:
                 if scan_opposing AND scan_score>=65: close += (score-65)/35*0.30
Status: USED but SEMANTICALLY WRONG (both planes, consistent). scan_score holds
        the OWN-direction zone conviction (e.g. 100), while scan_direction holds
        the OPPOSING bias. The CLOSE term fires the boost using the trade's own
        zone strength as if it were opposing-signal strength → a high-quality
        zone (100) maximises the CLOSE boost on any counter-bias trade.
```
```
Field: scan_direction
Type: str    Default: ""
Live source:   bootstrap:849-854,968 — wm.bias_dict()["direction"] (HTF bias)
Backtest src:  backtest:1811,1915 — bias.direction
Consumer(s):   decide_management:550-553 — scan_opposing test vs trade direction
Status: USED. Paired with scan_score (above) — the pairing is the bug, not the
        individual fields.
```
```
Field: live_oq / live_eq / entry_oq / entry_eq / oq_decay / eq_decay
Type: Optional[float]    Default: None
Live source:   bootstrap:909-949,969-974 — live_oq=wm.opportunity_quality,
               live_eq=entry_quality_for(wm,dir); entry_* captured on first eval;
               *_decay = entry - live
Backtest src:  backtest:1841-1843/1878-1882,1916-1921 — _oq_eq_from_wm(wm,dir);
               entry_* from trade dict; decay diffed
Consumer(s):   engine._oq_eq_decay_pressure:391-432 — CLOSE/TIGHTEN pressure when
               live_oq<floor or eq<floor or oq_decay significant
Status: USED (both planes). Inert (None) until quality layer populates OQ/EQ.
        NOTE backtest builder computes these TWICE (1841-1843 then 1878-1882) —
        redundant but the second assignment wins; harmless.
```
```
Field: d1_trend / d1_confidence / d1_event
Live source:   bootstrap:827,837,975-977
Backtest src:  backtest:1796,1803,1922
Consumer(s):   _compute_tf_alignment:439 (D1 weight 0.35); _compute_structure_
               integrity:598-605 (d1_opposing -0.30)
Status: USED (d1_event IS populated and read on the management plane — unlike
        the entry plane where it is omitted)
```
```
Field: h4_trend / h4_confidence / h4_event
Live source:   bootstrap:828,838,978-980      Backtest src: backtest:1797,1804,1923
Consumer(s):   _compute_tf_alignment:440 (H4 weight 0.30); _compute_structure_
               integrity:537-550 (h4_opposing -0.35 / supporting +0.20)
Status: USED
```
```
Field: h1_trend / h1_confidence / h1_event
Live source:   bootstrap:829,839,981-983      Backtest src: backtest:1798,1805,1924
Consumer(s):   _compute_tf_alignment:441 (H1 weight 0.20); _compute_structure_
               integrity:553-566 (h1_opposing -0.25 / supporting +0.15)
Status: USED (h1_event read on mgmt plane; NOT read on entry plane)
```
```
Field: h1_last_candle_bearish / h1_last_candle_doji
Type: Optional[bool] / bool    Default: None / False
Live source:   bootstrap:984-985 — _management_micro_context (H1@200 last closed)
Backtest src:  backtest:1809,1925-1926 — _h1_candle_context(H1 slice)
Consumer(s):   _compute_structure_integrity:569-577 (-0.10 when candle opposes)
Status: USED
```
```
Field: m1_trend / m1_event / m1_aligned_count
Live source:   bootstrap:871,986-988 — _management_micro_context (REAL M1@100)
Backtest src:  backtest:1808,1927-1929 — _micro_from_slice(M1 slice)
Consumer(s):   _compute_momentum:473-494 (candle + event); read_confidence:655
Status: USED (management M1 is live-computed in BOTH planes — contrast with the
        live ENTRY plane where m1_* fall through to defaults)
```
```
Field: m5_trend / m5_confidence / m5_event
Type: str/float/str    Default: "UNKNOWN"/0.0/"NONE"
Live source:   bootstrap:833,840,989-991 — _struct_trend_conf/_struct_event "M5"
Backtest src:  backtest:1802,1806,1930
Consumer(s):   _compute_tf_alignment:442 (M5 weight 0.15 — mgmt only);
               _compute_structure_integrity:582-595 (m5_opposing -0.20 / +0.10)
Status: USED (management-only fast feed; EntryContext has no M5 fields)
```
```
Field: fast_opposition_streak
Type: int    Default: 0
Live source:   bootstrap:867,992 — self._fast_opposition[order_id]
Backtest src:  backtest:1931 — trade["fast_opp"]
Consumer(s):   engine._fast_opposition_pressure:434-465 (bounded CLOSE pressure
               when streak >= min and not in profit)
Status: USED. Maintenance differs: backtest._update_fast_opposition:1939-1952
        uses the SIGN-CORRECT test (tf_align<-0.2 or momentum<-0.3 for both
        directions). The live increment lives elsewhere in the main loop; see
        Finding F8 for the sign-inversion risk on the live short side.
```
```
Field: score_history
Type: list[int]    Default: []
Live source:   bootstrap:860,993 — mgmt.score_history[-10:]
Backtest src:  backtest:1834-1835/1873-1874,1932 — appended per cycle
Consumer(s):   _compute_momentum trajectory:498-503; read_confidence:657;
               thesis conviction-collapse term (engine)
Status: USED
```
```
Field: open_trade_count
Type: int    Default: 0
Live source:   bootstrap:994 — len(open_positions)
Backtest src:  backtest:1935 — 1 (always, single-position serial)
Consumer(s):   governor.review heat/scale-in checks
Status: USED. MISMATCH (by design — serial backtest).
```
```
Field: max_open_trades
Type: int    Default: 5
Live source:   *** NOT SET *** → 5      Backtest src: backtest:1936 (config)
Consumer(s):   governor scale-in / portfolio checks
Status: DEFAULT-THROUGH (live uses literal default 5); MISMATCH backtest=config
```
```
Field: portfolio_heat_pct
Type: float    Default: 0.0
Live source:   bootstrap:900-907,995 — account_risk.heat (REAL)
Backtest src:  *** NOT SET *** → 0.0
Consumer(s):   governor.review:58,317 (scale-in heat cap, mgmt heat veto)
Status: USED (live). MISMATCH: backtest heat always 0.0 → heat-aware management
        never engages in backtest.
```
```
Field: session_name
Type: str    Default: "UNKNOWN"
Live source:   bootstrap:872-878,996 — session_engine status
Backtest src:  *** NOT SET in the return *** → "UNKNOWN" (computed at 1886-1893
               but session_name is NOT passed to TradeContext(); only
               session_tradeable is)
Consumer(s):   not read by decisions (label/cosmetic)
Status: DEAD-ish (cosmetic); MISMATCH live=real vs backtest="UNKNOWN"
```
```
Field: session_tradeable
Type: bool    Default: True
Live source:   bootstrap:873-878,997
Backtest src:  backtest:1848-1856/1887-1893,1934
Consumer(s):   _compute_urgency:633-636 (urgency 0.6 when not tradeable)
Status: USED
```
```
Field: minutes_to_high_impact_news
Type: float    Default: 999.0
Live source:   bootstrap:881-887,998 — news_guard.check (REAL)
Backtest src:  *** NOT SET *** → 999.0 (no historical news calendar)
Consumer(s):   _compute_urgency:627-632 (news urgency when <15min)
Status: USED (live). MISMATCH (by design): backtest news-urgency never fires.
```
```
Field: news_impact
Type: str    Default: "NONE"
Live source:   NOT SET → "NONE"      Backtest src: NOT SET → "NONE"
Consumer(s):   _compute_urgency:631 evidence string (cosmetic)
Status: DEAD (cosmetic, both planes)
```
```
Field: context_pressure / opposing_boost / pressure_details
Type: int/int/list    Default: 0 / 0 / []
Live source:   NOT SET → defaults      Backtest src: NOT SET → defaults
Consumer(s):   journal:81 logs context_pressure; no decision reads these
Status: DEAD (populated by neither builder; the
        `_compute_in_trade_context_pressure` referenced in the docstring at
        context.py:110 is not wired into either builder)
```
```
Field: confluences
Type: list    Default: []
Live source:   NOT SET → []      Backtest src: NOT SET → []
Consumer(s):   none in management decisions
Status: DEAD (management plane)
```
```
Field: d1_swing_high/low, h4_swing_high/low, h1_swing_high/low
Type: Optional[float]    Default: None (all six)
Live source:   *** NOT SET *** → None (all)
Backtest src:  *** NOT SET *** → None (all)
Consumer(s):   engine._set_protective_stop:888-906 — picks nearest structural
               swing for an adopted-trade protective stop
Status: DEAD / DEFAULT-THROUGH (both planes). Neither builder populates swing
        levels, so _set_protective_stop's candidate lists are always empty and
        it always returns None — the structure-based protective stop for adopted
        trades can never place a level.
```
```
Field: consensus_votes
Type: list[Vote]    Default: []
Live source:   bootstrap:844,999 — wm.votes_list()
Backtest src:  backtest:1861/1816,1933 — wm.votes_list()
Consumer(s):   assess_open_trade._assess_consensus:178,338-401 →
               consensus_alignment; decide_management:567-577 (CLOSE pressure
               when panel opposes)
Status: USED (both planes)
```

---

## Section 4 — Cross-Plane Comparison Table

Legend: **Same Source?** = same WorldModel-derived origin; **Same Value?** =
identical value for the same market state given how each builder populates it.

| Data point | EntryContext field | TradeContext field | Same source? | Same value? | Notes |
|---|---|---|---|---|---|
| D1 trend/conf | d1_trend/d1_confidence | d1_trend/d1_confidence | yes | yes | both via `_struct_trend_conf` |
| H4 trend/conf | h4_trend/h4_confidence | h4_trend/h4_confidence | yes | yes | |
| H1 trend/conf | h1_trend/h1_confidence | h1_trend/h1_confidence | yes | yes | |
| M5 trend/conf | — (no entry field) | m5_trend/m5_confidence | n/a | n/a | mgmt-only fast feed |
| D1 break event | d1_event | d1_event | helper same | **NO** | live entry omits → "NONE"; mgmt sets real |
| H4 break event | h4_event | h4_event | helper same | **NO** | live entry omits → "NONE"; mgmt sets real |
| H1 break event | h1_event | h1_event | helper same | **NO (live)** | live entry omits; mgmt sets real. (entry never reads h1_event anyway) |
| M5 break event | — | m5_event | n/a | n/a | mgmt-only |
| M1 alignment | m1_aligned_count | m1_aligned_count | **NO** | **NO** | entry(live)=const 3 (dict default); mgmt=live M1 count |
| M1 event | m1_event | m1_event | **NO** | **NO** | entry(live)="" (dict default); mgmt=live event |
| M1 trend | m1_trend | m1_trend | **NO** | **NO (live)** | entry(live)="UNKNOWN"; mgmt=live |
| H1 last candle | — | h1_last_candle_bearish/doji | n/a | n/a | mgmt-only |
| Zone type | entry_type | entry_type | partial | **NO** | entry=zone_type; mgmt(live)="" (unset) |
| Zone score | scan_score (own-dir) | scan_score (own-dir) | yes | similar | entry=conviction; mgmt=max own-dir zone conviction |
| Bias direction | bias_direction (DEAD) / scan_direction(unset) | scan_direction (bias) | partial | **NO** | entry carries bias in dead fields; mgmt uses bias as scan_direction |
| Consensus panel | consensus_votes | consensus_votes | yes | yes | full Vote panel reaches both |
| OQ/EQ | oq/eq (DEAD) | live_oq/live_eq/entry_*/decay | **NO** | **NO** | entry oq/eq never populated; mgmt computes live OQ/EQ |
| R:R | risk_reward_2 | (not on mgmt) | n/a | n/a | entry-only |
| Regime | regime | (not on mgmt) | n/a | n/a | entry(live)="" default; backtest=real |
| Portfolio heat | portfolio_heat_pct | portfolio_heat_pct | yes(live) | **NO** | live=real both; backtest entry & mgmt=0.0 |
| Spread | current_spread/typical_spread | (not on mgmt) | n/a | n/a | live=real; backtest=0.0 |
| Session tradeable | session_tradeable | session_tradeable | yes | partial | entry(live)=True default; mgmt(live)=real |
| News minutes | minutes_to_high_impact_news | minutes_to_high_impact_news | n/a | **NO** | entry(live)=999 default; mgmt(live)=real |
| Hold time | — | hold_minutes | n/a | n/a | mgmt-only |
| P&L | — | pnl_pips/pnl_dollars/profit_r | n/a | n/a | mgmt-only |
| Fast-opp streak | — | fast_opposition_streak | n/a | n/a | mgmt-only; sign convention differs live vs backtest (F8) |
| Swing levels | — | d1/h4/h1_swing_high/low | n/a | n/a | mgmt-only; never populated → all None |

---

## Section 5 — Findings

### 5.1 Dead fields (populated and/or defined but never read by any decision)

| Field | Plane | Why dead |
|---|---|---|
| `EntryContext.is_counter_trend` | entry | Populated live+backtest; DecisionEngine derives counter-trend from `h4_trend` via `_is_counter_htf` (engine.py:330) and never reads this flag. |
| `EntryContext.bias_direction` | entry | Populated; no consumer in `decision/`. |
| `EntryContext.oq` / `eq` / `scan_timestamp` | entry | Never populated, never read. |
| `EntryContext.risk_reward_1` | entry | Engine only reads `risk_reward_2`. |
| `EntryContext.scan_direction` | entry | Read only in `decide_management` (TradeContext). On the entry plane it is unread (and unset live / =direction backtest). |
| `EntryContext.ev_estimate` / `pair_multiplier` | entry | Logging only (journal:156-157). |
| `EntryContext.news_impact`, `TradeContext.news_impact` | both | Cosmetic evidence string. |
| `EntryContext.confluences`, `TradeContext.confluences` | both | Not read by any decision; journal/trade_journal only. |
| `TradeContext.context_pressure / opposing_boost / pressure_details` | mgmt | Neither builder populates them; `_compute_in_trade_context_pressure` not wired. Only `context_pressure` is logged. |
| `TradeContext.d1/h4/h1_swing_high/low` (6 fields) | mgmt | Never populated → `_set_protective_stop` (engine.py:888-906) candidate lists always empty → always returns None. |
| `EntryContext.h1_event` | entry | Even when backtest populates it, `assess_entry` checks only H4/D1 events. |

### 5.2 Mismatch fields (different value between planes for the same market state)

These are the live-vs-backtest divergences AND entry-vs-management divergences:

**A. Live-vs-Backtest on the ENTRY plane (backtest is RICHER than live):**
- `d1_event` / `h4_event` / `h1_event` — **backtest sets real BOS/CHOCH
  (backtest:1302-1304); live leaves them "NONE" (bootstrap:4775-4808 omits
  them).** This is the single most consequential divergence: the live entry
  `structure_integrity` never receives an HTF-break penalty
  (assess_entry:253-271), so the live entry plane is structurally blind to HTF
  breaks while backtest is not. Same market → different entry decision.
- `m1_aligned_count` — backtest = real M1 count; **live = constant 3** (the
  orchestrator `decision` dict never carries `m1_aligned`, so
  bootstrap:4792 default fires). Live entry candle-momentum is pinned at +0.10.
- `m1_event` — backtest = real; **live = ""** (dict never carries `m1_event`) →
  live entry momentum-event term never fires.
- `m1_trend` — backtest = real; live = "UNKNOWN" → live loses +0.15 read_confidence.
- `regime` — backtest = real H1 regime; live = "" → live always uses base
  DecisionWeights, backtest may use regime-specific weights.
- `account_balance` / `risk_pct` — backtest set; live unset (0.0). (Sizing is
  downstream, so low impact, but the contexts differ.)

**B. Live-vs-Backtest where LIVE is richer than backtest:**
- `portfolio_heat_pct` — live real (entry+mgmt); backtest 0.0 → backtest's
  governor heat dimension/veto never binds.
- `current_spread` / `typical_spread` — live real; backtest 0.0 → backtest's
  graded spread dimension is skipped (`typical_spread>0` guard false).
- `minutes_to_high_impact_news` — live real (mgmt); backtest 999 → backtest
  never sees news urgency (no historical calendar — accepted simplification).
- `open_trade_count` — live real; backtest 0 (entry) / 1 (mgmt) → portfolio
  concurrency constraints never bind in serial backtest (by design).
- `session_name`, `max_open_trades`, `entry_type` (mgmt) — minor unset/default
  differences (see Section 3).

**C. Entry-vs-Management within the LIVE path (same trade, seconds apart):**
- HTF break events reach **management** (`d1/h4/h1_event` set, bootstrap:977-983)
  but **not entry** (omitted). So at t=open entry sees `structure_integrity`
  high (no break penalty), and at t+interval management sees the SAME break and
  scores `structure_integrity` low → the enter-then-immediately-close pattern is
  a direct consequence of this asymmetric wiring, not a market change.
- Management additionally weights H1 events (situation.py:553) and M5 events
  (situation.py:582) that the entry plane never considers (entry checks only
  H4/D1, and only when populated — which live never is).

### 5.3 Default-through risks (field uses its default because nothing populates it)

| Field | Plane | Default fired | Effect |
|---|---|---|---|
| `d1_event/h4_event/h1_event` | live entry | "NONE" | HTF-break entry penalty never fires (5.2A). |
| `m1_aligned_count` | live entry | 3 | candle momentum pinned at +0.10 every trade. |
| `m1_event` | live entry | "" | momentum-event term never fires. |
| `m1_trend` | live entry | "UNKNOWN" | −0.15 read_confidence vs backtest. |
| `regime` | live entry | "" | base weights always (no regime weighting live). |
| `horizon` | both planes | "" | HTF demotion for SCALP/SWING never engages. |
| `entry_mode` | both planes | "PENDING" | MARKET fast-path unreachable from context builders. |
| `micro_confirmation` | both planes | "" | micro-confirmation MARKET term + sweep reversal evidence never fire. |
| `session_tradeable` | both entry | True | entry session-urgency never fires at the DE (EntryGate covers it). |
| `minutes_to_high_impact_news` | both entry | 999 | entry news-urgency never fires at the DE (EntryGate covers it). |
| `entry_type` | live mgmt | "" | `is_adopted` always False → adopted-observation branch unreachable live. |
| `max_open_trades` | live mgmt | 5 | literal default rather than config. |
| `portfolio_heat_pct` | backtest (both) | 0.0 | heat-aware governor logic inert in backtest. |
| `current/typical_spread` | backtest entry | 0.0 | graded spread dimension skipped in backtest. |
| `d1/h4/h1_swing_*` | both mgmt | None | adopted protective-stop never finds a level. |

### 5.4 Semantic-pairing bug (populated AND read, but the pairing is wrong)

- **`scan_score` ↔ `scan_direction` (management plane, both live & backtest).**
  `scan_score` = the highest conviction zone in the trade's **OWN** direction
  (bootstrap:861-866 / backtest:1866-1874); `scan_direction` = the **opposing**
  HTF bias (bootstrap:849-854 / backtest:1811). `decide_management:550-560` then
  treats `scan_score` as "opposing signal strength":
  `if scan_opposing and scan_score>=65: close += (scan_score-65)/35*0.30`.
  The trade's own high-quality zone (score 100) therefore drives the maximum
  CLOSE boost the moment HTF bias opposes — the own-direction strength is
  mis-read as opposing-signal strength. Present and consistent in BOTH planes.

### 5.5 Other structural notes

- **F8 — fast-opposition streak sign:** the backtest maintainer
  (`_update_fast_opposition`, backtest:1939-1952) uses the sign-correct test
  (`tf_align<-0.2 or momentum<-0.3`) for both directions because
  `assess_open_trade.tf_alignment` is already direction-normalized. The live
  increment is maintained elsewhere in the main loop (the value is read at
  bootstrap:867); if the live increment still uses a direction-branched
  (`is_long` vs `not is_long`) comparison it will be inverted for shorts. This
  trace did not locate a live increment site that matches the corrected
  backtest convention — confirm the live `self._fast_opposition` writer uses the
  same normalized test as backtest:1949.
- **Redundant computation (backtest mgmt builder):** `consensus_votes`,
  `current_score`/`score_history`, and `live_oq/live_eq/decay` are each computed
  twice (backtest:1816 & 1861; 1822-1835 & 1866-1874; 1841-1843 & 1878-1882).
  The second assignment wins; behaviour is correct but the duplication is dead
  work and a maintenance hazard.
- **`consensus_aware` default True** (engine.py:107) and both builders populate
  `consensus_votes`, so the directional-consensus dimension is live on both
  planes — the one piece of "full market evidence" that reaches both entry and
  management identically.

### 5.6 One-line summary

The **management** plane is fed a rich, live, WorldModel-derived `TradeContext`
(HTF + M5 + M1 + consensus + OQ/EQ + session/news + heat). The **live entry**
plane is fed a `EntryContext` that is **missing every HTF break event, all live
M1 data, regime, and several urgency inputs** (they fall through to defaults),
whereas the **backtest entry** plane populates those same fields for real. The
result: (1) live entry is structurally blind to HTF breaks that management acts
on seconds later, and (2) backtest entry evaluates a strictly richer context than
live entry — so backtest and live can take different entry decisions on identical
market data.

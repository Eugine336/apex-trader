# PROMPT FOR THE NEXT IMPLEMENTER — Apex Trader full redesign (continue until it is complete)

Paste this whole file to the next engineer or AI session. It tells you what Apex is meant to be, what has happened so far,
what is wrong or unproven, what to do next, and where the truth lives. Do not start coding until you have read §1–§6 and run §9.

## 1. What Apex is meant to be (the owner's words, the canonical identity)
Apex is one experienced discretionary trader, not a bot. It has all trading knowledge and intelligence about every tool,
concept (ICT, SMC, CRT and the rest), strategy, indicator (RSI, EMA, ...), price action, pair and instrument. It looks at the
full market state like a normal trader and decides *this state needs this knowledge*, analyses with it, and trades, manages
and closes positions to profitability. It is not told which concept or strategy to use, it has **no threshold or configuration
values for analysis**, it is neither a scalper nor a swing trader, it carries **no direction bias**, and **the market is the
only source of truth**. It keeps watching every enabled instrument, open or not, and learns from every close. The account owner
sets the hard execution limits; Apex decides within them from its analysis. The native Brain must be excellent *without* an
external AI provider (providers have budgets and can be down; they are advisory only).
Canonical text: `cognition/identity.py` and `docs/APEX_IDENTITY.md`. Data comes **only from the MetaTrader 5 Python API** on the
owner's host; whatever MT5 cannot supply stays `unknown`/data-gated (see `docs/APEX_MT5_DATA_CAPABILITY_CONTRACT.md`).

## 2. What happened (short history, so you do not redo or undo it)
1. The repository started as a rule-based, Gold-only, multi-source trading bot with legacy entry stacks and dormant subsystems.
2. Over many PRs (several people/sessions working in parallel): the Gold-only restriction was removed (default universe is all
   instruments); the legacy entry stack (EntryEngine, gate tuner, news planner, pending-order staging, quality layer) and the RL
   subsystem were deleted; `ComplianceDivision` was found to be *never called* and was wired into live origination (fails
   closed); the market observer was made instrument-agnostic; calibration was changed to **scale risk, never gate eligibility**.
3. A **concept framework** (`cognition/concepts/`) turned "measurements" into **full readings** (applicability, per-side
   implication, strength vs the instrument's own history, trigger, invalidation, objective, milestones, requires, management
   meaning) and was wired through evidence → hypotheses → geometry → management. Other sessions then built the remaining readers,
   lens compositions, cache/performance work, evidence-lineage gates and Brain-authored management.
4. Today: **every one of the 79 analytical methods and 38 strategy lenses has a reader.** 56 methods and 20 lenses are fully
   readable from price bars; 23 methods and 18 lenses are `DATA-GATED` on a named feed (`docs/APEX_CONCEPT_COVERAGE.md`,
   generated; 30 registered concept modules).
5. This last stretch focused on the **MT5 data path**, where real defects were found:
   - Tick history was normalized as if MT5 returned broker-server time; the owner host's evidence shows history rows are UTC.
   - Bars were labelled UTC although MT5 labels them in broker-server time. On a non-UTC broker (the owner host is ~UTC+2) every
     bar looked hours in the future, every concept reading failed its freshness check and geometry was never derived — **no
     trade could ever be built**. (Reproduced: ±7200 s and +10800 s offsets gave 101 readings and no geometry.)
   - Both were fixed by *proving the time domain from the response/terminal data* instead of assuming it (PR #910).
   - The broker names gold `XAUUSD.ecn`; the symbol mapping exists but is not proven on every production path.
6. A quality harness (PR #909) found direction biases (fixed for 4 modules) and 7 more modules that still fail mirror symmetry.

**Nothing has run end to end on a real feed.** All results are fakes, synthetic candles and owner-host *diagnostic* captures.
No system in the roadmap is "accepted". Merge ≠ acceptance.

## 3. Current state (verify; numbers move)
- `main` @ `8e316a3a` at the time of writing; open PRs: **#909** (quality harness + 3 bias fixes) and **#910** (WP-T3/T4 tick and
  bar time-domain proofs, bounded tick gap recovery, ledger entry, next-work handoff). Merge/rebase them first.
- Full suite on `main` + those branches: ~4,619 passed, 7 strict xfails (known mirror defects), 0 failed.
- Coverage register: methods FULL 56 / DATA-GATED 23; lenses FULL 20 / DATA-GATED 18.

## 4. What must happen next (priority order; full detail and file pointers in `docs/APEX_NEXT_WORK_HANDOFF.md`)
1. **WP-T6** durable tick cursor + restart recovery (+ restart test matrix). 2. **WP-T2** terminal lifecycle and clock witnesses
(invalidate cached proofs on identity/offset change). 3. **WP-T5** downstream consumers and candle integrity (verified frames
only, completed bars only, continuity/duplicate/out-of-order checks, DST-offset validity for older bars).
4. **Owner-host verification** on the owner host and a second host across a DST change (ticks, bars, `XAUUSD.ecn` symbol path);
record evidence in the ledger — only then can T-A1…T-A10 leave OPEN. 5. **Multi-symbol bar channel** (cross-asset, currency
strength, SMT, pairs). 6. Fix the **7 mirror-symmetry defects** and classify ~100 fixed window constants (definition vs
parameter). 7. Build the **MT5-derivable data-gated concepts** (market impact from own fills, carry from swap fields, portfolio
risk from positions/margin, Bayesian/Monte Carlo/calibration from accumulated broker-truth outcomes). 8. **System acceptance**
for Systems 1–10 and a **read-only-first supervised demo run** on the owner host.

**After those fixes the redesign continues until the end.** Do not stop at WP-T. The end state is §10. Keep pulling work from
`docs/APEX_FULL_REDESIGN_ROADMAP.md` (system-by-system acceptance), `docs/APEX_CONCEPT_IMPLEMENTATION_PLAN.md` (concept and Brain
work packages WP-A…WP-J, WP-B1…B7) and the ledger's open items until every acceptance gate is closed with evidence.

## 5. Where the truth lives (read in this order; docs can be stale — verify against code and tests)
| File | What it is |
|---|---|
| `cognition/identity.py`, `docs/APEX_IDENTITY.md` | the canonical identity (what Apex must be) |
| `docs/APEX_NEXT_WORK_HANDOFF.md` | the prioritised next work with file pointers and acceptance mapping |
| `docs/APEX_FULL_REDESIGN_ROADMAP.md` | the 10-system roadmap, rules, acceptance gates, status register |
| `docs/APEX_ENGINEERING_DELIVERY_CONTRACT.md` | how work is delivered and accepted |
| `docs/APEX_ENGINEERING_PROGRESS_LEDGER.md` | running log of what was done, evidence, and what is still open (append, never rewrite history) |
| `docs/WP_MT5_CLOCK_AUTHORITY_TICK_STREAM_INTEGRITY.md` | WP-T1…T6 specification and the T-A1…T-A10 acceptance matrix (all OPEN) |
| `docs/APEX_MT5_DATA_CAPABILITY_CONTRACT.md`, `docs/MT5_CLOCK_BRIDGE.md`, `docs/APEX_DATA_SOURCE_AUDIT.md` | what MT5 can and cannot supply; clock authority; data-source audit |
| `docs/APEX_CONCEPT_COVERAGE.md` | generated register of every method/lens (`python scripts/concept_coverage.py`) |
| `docs/APEX_CONCEPT_IMPLEMENTATION_PLAN.md`, `docs/APEX_REMAINING_WORK_PROMPT.md` | concept/Brain work packages and the rules for implementers |
| `docs/APEX_TARGET_TRADING_CYCLES.md` | five target-state full trading cycles (design, not executed behaviour) |

## 6. Working protocol (non-negotiable)
1. `git fetch`; branch from the latest `main` (or from the open PR you are extending). Pull again before you open a PR.
2. **Run the full suite on a clean checkout of `main` first, then on your branch**, and list every test that fails on yours but
   not on `main`. Two earlier merged PRs shipped broken syntax/tests because this was skipped. Environment-only failures must be
   named as such, not hidden.
3. One work package per PR. Update the ledger (append) and any status register you change. Regenerate the coverage register.
4. Report with evidence: tests run and counts, what you did **not** verify, decisions needed from the owner. Never claim behaviour
   you have not executed; never write "accepted" without the evidence the WP's acceptance item requires.
5. Never relax a safeguard to make a test pass; never delete a failing test to hide a failure; never loosen a gate to make a demo
   look good.
6. No live broker mutation during engineering. No secrets or tokens in code, docs, commits or PR text.

## 7. Hard design rules
Brain is the only decision authority; concept code returns evidence with `polarity=0` and never opens, sizes, exits or vetoes.
Completed bars only, no look-ahead. **No configured thresholds in analysis**: strength is a percentile rank against the
instrument's own history; the only constants allowed are definitions of the concept itself, commented as such. **Unknown stays
unknown** — missing/untrusted data produces `None`/`unknowns`, never a default. Invalidation is a price or event; objectives are
earlier structural levels that exist in the data. Instrument-agnostic (no symbol/asset-class branches in analysis). Faults are
visible (WARNING), no silent `except: pass`. Time: **never assume a timestamp domain — prove it** (ticks and bars both). Owner
limits are execution limits, separate from analysis; Apex may tighten them from its analysis, never loosen them.

## 8. Pitfalls already seen
Merged PRs with stale tests/syntax breaking `main`; a component constructed and logged as live but never called (compliance,
the old entry engine); fallbacks that silently produced "fail-open" evidence; timestamp domains assumed instead of proven;
random-walk fixtures containing accidental patterns (use deterministic fixtures); tie-breaking by the letter H/L creating
direction bias; the sandbox filesystem resets between sessions (push often); the GitHub API rate-limits (use git history).

## 9. First 30 minutes
```
git fetch && git switch -c <your-wp>/<name> origin/main      # or the PR branch you extend
pip install -r requirements.txt                              # MetaTrader5 is Windows-only; tests use fakes
python -m pytest -q tests --continue-on-collection-errors    # record the baseline
python scripts/concept_coverage.py --check
sed -n 1,80p docs/APEX_NEXT_WORK_HANDOFF.md
grep -n "OPEN" docs/WP_MT5_CLOCK_AUTHORITY_TICK_STREAM_INTEGRITY.md   # T-A1..T-A10
```

## 10. Definition of done for the whole redesign
All ten systems accepted with evidence; WP-T T-A1…T-A10 closed with owner-host (and second-host) evidence; every catalogued
method and lens `FULL` or explicitly `DATA-GATED` with the missing feed named and approved; no concept failing the quality
harness; the Brain composes concept readings by context, builds geometry and manages positions from concept levels, with owner
limits as the only hard analysis-independent constraints; hindcast and live outcome records feed per-concept track records that
Apex consults but that never gate a thesis; a recorded supervised demo run on the owner host showing the full cycle
(observe → concept readings → hypotheses → geometry → compliance → sizing → execution → management → close → learning).
Until that run exists, nothing here is "complete".

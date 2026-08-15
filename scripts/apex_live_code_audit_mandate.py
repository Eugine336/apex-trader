#!/usr/bin/env python3
"""Emit the APEX live-code constitutional audit mandate.

This is intentionally executable, not a README substitute.  The mandate is the
operator prompt for engineers/agents who are about to audit or reconstruct the
*running code path* against the APEX philosophy.  Documentation, comments and
claimed architecture can provide context, but this mandate makes raw execution
behavior the only compliance evidence.
"""

from __future__ import annotations

MANDATE = """APEX — LIVE CODE CONSTITUTIONAL AUDIT & RECONSTRUCTION MANDATE

PRIMARY DIRECTIVE
AUDIT THE LIVE RAW CODE AGAINST THE COMPLETE APEX PHILOSOPHY.

The code is the authority. Not the README, architecture documents, comments,
docstrings, design documents, tickets, TODOs, intended behavior, commit messages,
variable names, claimed architecture, developer explanations, or previous audits.
Those may be consulted only for context. They are never evidence that behavior
exists.

CORE PHILOSOPHY TO ENFORCE
1. APEX trades opportunities, not indicators.
2. The market is the source of truth; do not destroy market information before cognition.
3. Analytical modules are instruments, not traders; they emit observations, never LONG/SHORT votes.
4. Higher timeframe is context, not command.
5. APEX must discover opportunities that were never explicitly programmed.
6. Direction is a consequence of cognition, not the cognitive representation.
7. The Brain must maintain primary, alternative, and counter-hypotheses.
8. FLAT means no sufficiently exploitable opportunity after EV, risk, execution,
   hypotheses, and market state are considered — never merely mixed indicators.
9. APEX harvests executable positive-EV opportunities across all horizons.
10. The market does not owe APEX one coherent direction.
11. Give the Brain rich market state, not pre-collapsed LONG 0.55 summaries.
12. The council is independent reasoners, not directional voters.
13. Advisor failure degrades capacity explicitly; it must not destroy cognition.
14. Local inference is the long-term sovereign cognitive foundation.
15. Storage is not compute; local model storage must still route through CPU/RAM/VRAM inference.
16. Execution is downstream of cognition and risk governance.
17. Management is continuous cognition, not merely trailing stops.
18. Live positions are continuously compared against the best current opportunity.
19. Risk, spread, slippage, liquidity, exposure, and execution feasibility remain mandatory.
20. Live architecture must flow: market → state → observations → opportunity engine → council → hypotheses → EV → risk → execution → management → market.
21. Provider constraints are operating reality: rate limits, RPM, TPM, RPD, TPD,
   monthly/free quotas, concurrency, context/output limits, 429/5xx/timeouts,
   outages, model removal, auth failure, credit exhaustion, regional limits, and dynamic capacity.
22. Every provider/model combination is a known finite resource pool, not merely a URL.
23. Free models are constrained resources, never unlimited resources.
24. Every AI request has an estimated resource cost before it is sent.
25. Provider routing is dynamic, health-aware, quota-aware, latency-aware, capability-aware, and resource-longevity-aware.
26. Provider health is live state: HEALTHY, DEGRADED, THROTTLED, QUOTA_LOW,
   QUOTA_EXHAUSTED, TIMEOUTING, UNAVAILABLE, AUTH_FAILURE, MODEL_UNAVAILABLE, COOLDOWN.
27. Quota recovery/reset must be represented and providers must automatically rejoin after recovery probes.
28. Scarce remaining quota must not be spent blindly; request value matters.
29. Council degradation must be explicit; never fabricate missing advisors.
30. AI infrastructure failure must not equal APEX failure.
31. Cognitive availability and execution/risk availability are separate.
32. APEX must continuously know its own cognitive capacity.
33. Provider diversity is architectural; no single provider may be indispensable.
34. Provider accounting must be observable: provider, model, requests, tokens,
   remaining quota, failures, 429s, timeouts, latency, cooldown, reset, consumer, cost, and routing reason.
35. The provider router must not become a hidden bottleneck.
36. Continuous APEX availability means architectural resilience, not impossible mathematical uptime.
37. APEX is a resilient pool of interchangeable cognitive resources feeding cognition, risk, execution, and management.

AUDIT EXECUTION PATHS, NOT FILE NAMES
Trace real runtime flow for every important behavior:
INPUT → INGESTION → TRANSFORMATION → ANALYSIS → COGNITION → COUNCIL → BRAIN → RISK → EXECUTION → POSITION → MANAGEMENT → EXIT.
Follow function calls, events, callbacks, async paths, queues, caches, persistence,
state mutation, fallbacks, retries, bypasses, legacy paths, race conditions, stale
state, silent defaults, provider-routing failures, and failure paths.

FIND EVERY VIOLATION
Report every architectural, behavioral, cognitive, data, decision, temporal,
risk, execution, management, infrastructure, resource-accounting, failure-handling,
and semantic violation. The standard is not mostly implemented. If one reachable
line, branch, default value, enum, schema field, fallback, retry, provider adapter,
management rule, or legacy path contradicts the constitution, it is a violation.

PRIORITY AUDIT TARGETS
- Premature compression into LONG, SHORT, FLAT, direction, confidence, vote,
  quorum, consensus, alignment, bias, trend, or low-dimensional score before cognition.
- Directional models that implement evidence → direction → confidence → trade.
- Data collected but not delivered to the Brain.
- Council members that are actually indicator voters.
- Brain schemas that cannot represent market state, hypotheses, counter-hypotheses,
  opportunity, confirmation, invalidation, EV, risk, execution quality, horizon,
  uncertainty, and what would change the thesis.
- FLAT/NO_OPINION/REJECT/NO_TRADE paths that hide mixed indicators, provider
  failures, low directional confidence, or infrastructure degradation.
- Management paths that mechanically close/hold/reverse without renewed opportunity cognition.
- Provider integrations lacking live RPM/TPM/RPD/TPD/quota/concurrency/context/
  output/latency/failure/cooldown/reset accounting.
- Free-tier strategies that burn scarce quota blindly or let one exhausted provider
  take down the council.
- Local inference paths that are not first-class interchangeable providers.
- Any path where AI failure can disable mandatory risk protection.
- Fixed loops that call expensive models without meaningful market change or reason on stale state.
- Legacy decision engines, signal systems, polling systems, wrappers, schemas, and dead-but-reachable paths.

RECONSTRUCTION AUTHORITY
After violations are proven from code, redesign, refactor, rewire, replace,
remove, delete, merge, split, or restructure whatever is necessary. Do not patch
symptoms. Do not preserve incorrect architecture because it is inconvenient or
because a README describes it.

VIOLATION REGISTER FORMAT
VIOLATION ID:
SEVERITY: CRITICAL | HIGH | MEDIUM | LOW
PHILOSOPHY RULE:
ACTUAL CODE BEHAVIOR:
EXACT EXECUTION PATH:
WHY IT VIOLATES THE PHILOSOPHY:
DATA LOST / AUTHORITY MISPLACED:
ROOT CAUSE:
REQUIRED CHANGE:
FILES / SYMBOLS INVOLVED:
DEPENDENCIES:
REGRESSION RISKS:
VERIFICATION METHOD:
STATUS:

NO UNVERIFIED CLAIMS
Do not write “should work”, “appears compliant”, “probably receives the data”,
or “seems independent”. Prove it from code. If it cannot be proven, mark it
UNVERIFIED.

FINAL ACCEPTANCE CRITERION
The audit is complete only when live code demonstrates raw/structured market
reality reaches cognition; modules provide evidence not votes; no premature
LONG/SHORT/FLAT compression exists; higher timeframe is context; the Brain forms
multiple hypotheses and discovers opportunities; FLAT means no exploitable
opportunity; council reasoners are independent; EV and execution feasibility
matter; risk survives AI outages; management is continuous cognition; provider
quotas are accounted for; provider failures are isolated; local inference is
first-class; expensive reasoning is triggered intelligently; live state is fresh;
legacy contradictions are gone; and every reachable execution path respects the
constitution.

FINAL COMMAND
Do not audit whether APEX resembles this philosophy. Audit whether APEX IS this
philosophy in executable code. The documentation does not define compliance. The
live code does.
"""


def main() -> int:
    print(MANDATE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

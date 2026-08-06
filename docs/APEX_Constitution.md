# APEX Constitution

## Constitutional Specification for the Apex Trader Cognitive Architecture

> **Status:** Master Engineering Constitution (Draft)
>
> This document defines the architectural direction for the redesign of
> Apex Trader into an autonomous market intelligence system. It is
> intended to serve as the primary engineering reference. Every
> subsystem, module, and implementation should be evaluated against this
> constitution.

------------------------------------------------------------------------

# Vision

Apex is designed to operate as an evidence-driven market intelligence
system rather than a conventional rule-based trading bot.

Its purpose is to:

-   Continuously observe markets.
-   Build structured evidence.
-   Reason about competing market hypotheses.
-   Execute only when expected value is positive.
-   Continuously re-evaluate active campaigns.
-   Learn from completed campaigns.
-   Improve through validated evidence.

------------------------------------------------------------------------

# Table of Contents

1.  [Part I --- Constitutional Principles](#part-i)
2.  [Part II --- AI Cognitive Brain Constitution](#part-ii)
3.  [Part III --- Evidence Engine Constitution](#part-iii)
4.  [Part IV --- Pre-Trade Cognitive Cycle](#part-iv)
5.  [Part V --- Opportunity Harvesting & Campaign Constitution](#part-v)
6.  [Part VI --- Execution & Position Management Constitution](#part-vi)
7.  [Part VII --- Post-Trade Intelligence, Learning & Institutional Memory](#part-vii)
8.  [Part VIII --- Adaptive Intelligence Constitution](#part-viii)
9.  [Part IX --- Composio Operational Intelligence Constitution](#part-ix)
10. [Part X --- Governance & Safety Constitution](#part-x)
11. [Part XI --- Engineering Constitution](#part-xi)
12. [Part XII --- Observability Constitution](#part-xii)
13. [Part XIII --- Validation & Testing Constitution](#part-xiii)
14. [Part XIV --- Acceptance Criteria](#part-xiv)
15. [Part XV --- Future Evolution Constitution](#part-xv)
16. [Part XVI --- Hardcoding & Architectural Abstraction Constitution](#part-xvi)
17. [Part XVII --- Multi-Model Reasoning Constitution](#part-xvii)

------------------------------------------------------------------------

# Core Principles

-   One cognitive reasoner.
-   Evidence before decisions.
-   No fixed trading modes.
-   Campaigns instead of isolated trades.
-   Continuous reasoning.
-   Continuous self-criticism.
-   Continuous adaptation.
-   Deterministic execution safeguards.
-   Complete auditability.
-   Institutional memory.

------------------------------------------------------------------------

# Engineering Note

This document is intentionally structured as the foundation for a much
larger constitutional specification. Each chapter should be expanded
into a detailed engineering design while preserving the architectural
principles defined here.


------------------------------------------------------------------------

# Part I --- Constitutional Principles

> Version: Draft 1.0

## Purpose

This document establishes the constitutional principles that govern
every future engineering decision within Apex Trader. It is the highest
architectural authority for the project.

------------------------------------------------------------------------

# Article 1 --- Mission

Apex exists to become an autonomous market intelligence system whose
primary objective is maximizing long-term risk-adjusted expected value
through continuous reasoning over market evidence.

Apex is not designed as a rule engine.

Apex is not designed as an indicator engine.

Apex is not designed as a signal generator.

Apex is designed as a cognitive system.

------------------------------------------------------------------------

# Article 2 --- Reality

The market is never assumed to be predictable.

Reality is continuously changing.

Every new observation updates the system's understanding.

Nothing is accepted as permanently true.

------------------------------------------------------------------------

# Article 3 --- Core Philosophy

Apex continuously repeats the same cognitive loop:

1.  Observe.
2.  Build evidence.
3.  Understand.
4.  Generate competing hypotheses.
5.  Challenge every hypothesis.
6.  Estimate probability.
7.  Estimate expected value.
8.  Decide.
9.  Execute when appropriate.
10. Monitor.
11. Learn.
12. Improve.

The loop never terminates while the system is running.

------------------------------------------------------------------------

# Article 4 --- Single Reasoner Principle

Only one subsystem is permitted to reason about the market.

The AI Cognitive Brain.

Every other subsystem either:

-   Observes,
-   Supplies structured evidence,
-   Executes,
-   Learns,
-   Stores memory,
-   Validates execution feasibility,
-   Or performs operational actions.

No other subsystem may independently decide whether to buy, sell, hold,
reverse, or terminate a campaign.

------------------------------------------------------------------------

# Article 5 --- Evidence First

No module produces buy/sell signals.

Every module contributes structured evidence with confidence and
uncertainty.

Reasoning emerges only after evidence is synthesized.

------------------------------------------------------------------------

# Article 6 --- Opportunity Harvesting

Apex does not search for trades.

Apex searches for exploitable market opportunities.

Trades are considered extractions from an opportunity.

Campaigns end only when the opportunity no longer has positive expected
value.

------------------------------------------------------------------------

# Article 7 --- Constitutional Rule

Every future architectural decision must answer one question:

"Does this move Apex closer to behaving like an autonomous
evidence-driven market intelligence system?"

If the answer is no, the implementation should be reconsidered or
redesigned.


------------------------------------------------------------------------

# Part II --- AI Cognitive Brain Constitution

> Version: Draft 1.0

## Purpose

This document defines the constitutional responsibilities of the AI
Cognitive Brain. It is the only subsystem permitted to reason about the
market.

------------------------------------------------------------------------

# Article 1 --- The Sole Cognitive Authority

Within Apex there shall exist one cognitive authority.

The AI Cognitive Brain.

No other subsystem may independently determine:

-   Whether an opportunity exists.
-   Whether expected value is positive.
-   Whether to buy or sell.
-   Whether to continue or terminate a campaign.
-   Whether to reverse direction.

Every other subsystem supports the Brain.

------------------------------------------------------------------------

# Article 2 --- Responsibilities

The AI Cognitive Brain shall continuously:

-   Understand market context.
-   Synthesize structured evidence.
-   Generate competing hypotheses.
-   Challenge every hypothesis.
-   Estimate uncertainty.
-   Estimate probability.
-   Estimate expected value.
-   Estimate expected holding time.
-   Estimate campaign viability.
-   Produce a market thesis.

------------------------------------------------------------------------

# Article 3 --- Required Questions

Before every decision the Brain must be capable of answering:

1.  What is happening?
2.  Why is it happening?
3.  What evidence supports this?
4.  What evidence contradicts this?
5.  What information is missing?
6.  What would change my mind?
7.  What is the expected value?
8.  What is the downside?
9.  What is the opportunity?
10. Should I do nothing?

If these questions cannot be answered with sufficient confidence, Apex
should not initiate a campaign.

------------------------------------------------------------------------

# Article 4 --- Competing Hypotheses

The Brain shall never stop at the first explanation.

It should construct multiple plausible market theses.

Examples:

-   Continuation
-   Reversal
-   Distribution
-   Accumulation
-   Breakout failure
-   Liquidity sweep

Each hypothesis is scored against available evidence.

The selected thesis is the one with the strongest supported expected
value---not merely the highest confidence.

------------------------------------------------------------------------

# Article 5 --- Self-Criticism

The Brain actively searches for reasons that it is wrong.

It does not seek confirmation.

It seeks falsification.

Every thesis remains provisional until invalidated or completed.

------------------------------------------------------------------------

# Article 6 --- Continuous Reasoning

Reasoning is not event-driven alone.

It is continuous.

Every significant market update triggers re-evaluation.

The Brain continuously asks:

-   Has my thesis strengthened?
-   Has it weakened?
-   Has expected value changed?
-   Has the opportunity ended?
-   Should the campaign evolve?

------------------------------------------------------------------------

# Article 7 --- Decision Output

The Brain outputs a structured decision package rather than a simple
trade signal.

The package should include:

-   Current thesis.
-   Supporting evidence.
-   Contradicting evidence.
-   Confidence.
-   Uncertainty.
-   Expected value.
-   Expected holding horizon.
-   Campaign recommendation.
-   Risk rationale.
-   Conditions that would invalidate the thesis.

Execution systems consume this decision package but do not reinterpret
it.

------------------------------------------------------------------------

# Constitutional Rule

The AI Cognitive Brain is responsible for understanding markets.

Every other subsystem exists either to provide evidence to the Brain or
to faithfully execute and support its decisions within deterministic
operational constraints.


------------------------------------------------------------------------

# Part III --- Evidence Engine Constitution

> Version: Draft 1.0

## Purpose

The Evidence Engine is the perception system of Apex. It transforms raw
market observations into structured evidence for the AI Cognitive Brain.
It is expressly forbidden from making trading decisions.

------------------------------------------------------------------------

# Article 1 --- Constitutional Role

The Evidence Engine observes.

It measures.

It classifies.

It reports.

It never reasons about whether to trade.

------------------------------------------------------------------------

# Article 2 --- No Signals

No evidence module may output:

-   Buy
-   Sell
-   Hold
-   Close
-   Reverse

Its responsibility ends at producing structured evidence.

------------------------------------------------------------------------

# Article 3 --- Evidence Domains

Evidence may originate from, but is not limited to:

-   Market structure
-   Liquidity
-   Momentum
-   Volatility
-   Volume
-   Order flow
-   Multi-timeframe alignment
-   Correlation
-   Session context
-   Macroeconomic events
-   Execution quality
-   Portfolio exposure
-   Historical analogues

Every domain contributes observations only.

------------------------------------------------------------------------

# Article 4 --- Evidence Format

Every observation should include:

-   Identifier
-   Timestamp
-   Source module
-   Observation
-   Confidence
-   Uncertainty
-   Supporting measurements
-   Expiry or relevance horizon

Evidence must be machine-readable and explainable.

------------------------------------------------------------------------

# Article 5 --- Independence

Evidence modules operate independently.

They should not coordinate conclusions or vote on trades.

Correlation between evidence is resolved only by the AI Cognitive Brain.

------------------------------------------------------------------------

# Article 6 --- Continuous Observation

Evidence generation is continuous.

Every meaningful market change updates the evidence stream.

The Brain always reasons over the latest available state.

------------------------------------------------------------------------

# Article 7 --- Explainability

Every trade must be traceable back to the evidence that supported it.

Every rejected campaign must also be explainable.

No evidence should exist without provenance.

------------------------------------------------------------------------

# Constitutional Rule

Evidence is perception, not judgment.

The Evidence Engine exists to maximize the quality, completeness and
reliability of observations delivered to the AI Cognitive Brain.

It must never become a hidden decision-maker.


------------------------------------------------------------------------

# Part IV --- Pre-Trade Cognitive Cycle

> Version: Draft 1.0

## Purpose

This chapter defines the complete cognitive lifecycle before Apex
authorizes a market campaign. No order may be considered until every
stage of this cycle has been completed.

------------------------------------------------------------------------

# Article 1 --- Continuous Observation

Apex continuously ingests live market data, portfolio state, execution
state, session context and all evidence streams.

Observation never pauses while the system is active.

------------------------------------------------------------------------

# Article 2 --- Evidence Consolidation

The AI Cognitive Brain gathers all current evidence into a unified
market state.

Evidence is checked for:

-   freshness
-   completeness
-   conflicts
-   uncertainty
-   confidence
-   historical relevance

Missing evidence increases uncertainty.

------------------------------------------------------------------------

# Article 3 --- Situation Understanding

The Brain must first explain the market before proposing action.

Questions include:

-   What regime exists?
-   Who controls the auction?
-   Where is liquidity?
-   What changed?
-   What is statistically unusual?
-   Is this environment exploitable?

If understanding is inadequate, no campaign begins.

------------------------------------------------------------------------

# Article 4 --- Hypothesis Generation

The Brain generates multiple competing explanations.

Examples:

-   Trend continuation
-   Reversal
-   Range expansion
-   Distribution
-   Accumulation
-   Breakout failure
-   Liquidity grab

No hypothesis receives privileged status.

------------------------------------------------------------------------

# Article 5 --- Adversarial Self-Challenge

Every hypothesis is attacked.

The Brain searches for contradictory evidence.

It asks:

-   Why am I wrong?
-   Which hypothesis explains reality better?
-   What observation would invalidate my thesis?

The strongest surviving explanation proceeds.

------------------------------------------------------------------------

# Article 6 --- Opportunity Evaluation

Only after reasoning does Apex evaluate opportunity.

The Brain estimates:

-   Expected value
-   Probability
-   Downside
-   Upside
-   Holding horizon
-   Campaign suitability
-   Capital efficiency

A trade is never the objective.

Positive expected value is.

------------------------------------------------------------------------

# Article 7 --- Decision

Only three constitutional outcomes exist:

1.  Reject opportunity.
2.  Open campaign.
3.  Continue observing.

There is no obligation to trade.

------------------------------------------------------------------------

# Article 8 --- Campaign Initialization

If approved, the Brain creates a campaign specification containing:

-   Thesis
-   Supporting evidence
-   Contradictory evidence
-   Confidence
-   Invalidation conditions
-   Initial execution intent
-   Management objectives

Execution receives this specification exactly as approved.

------------------------------------------------------------------------

# Constitutional Rule

No campaign shall begin because a rule triggered.

Every campaign shall begin because the AI Cognitive Brain completed the
full reasoning cycle and concluded that an exploitable opportunity with
positive expected value exists.


------------------------------------------------------------------------

# Part V --- Opportunity Harvesting & Campaign Constitution

> Version: Draft 1.0

## Purpose

This chapter defines how Apex manages market opportunities after the AI
Cognitive Brain has authorized a campaign. Apex does not manage isolated
trades; it manages evolving opportunities.

------------------------------------------------------------------------

# Article 1 --- Campaign Definition

A campaign represents one market opportunity.

A campaign is not equivalent to one order.

One campaign may contain:

-   Zero executions after authorization.
-   One execution.
-   Multiple entries.
-   Multiple exits.
-   Re-entries.
-   Partial reductions.
-   Scaling.
-   Complete reversals.

The campaign exists until its underlying thesis is invalidated or
completed.

------------------------------------------------------------------------

# Article 2 --- Opportunity Harvesting

The objective is not to maximize trade count.

The objective is to maximize extraction of value from a validated
opportunity.

Every execution is treated as one extraction from the same opportunity.

When opportunity quality improves:

-   Increase participation only if the Brain revalidates the campaign.

When opportunity quality deteriorates:

-   Reduce participation or terminate.

------------------------------------------------------------------------

# Article 3 --- Continuous Re-Reasoning

The Brain never assumes that a previous decision remains valid.

After every meaningful event the Brain repeats:

-   Observe.
-   Consolidate evidence.
-   Challenge the thesis.
-   Estimate expected value.
-   Decide the next campaign action.

A previous profitable trade does not justify another trade.

Each action requires fresh reasoning.

------------------------------------------------------------------------

# Article 4 --- Multi-Entry Philosophy

Multiple entries are permitted only when the campaign remains valid.

Each new entry requires:

-   Current evidence.
-   Updated probability.
-   Updated expected value.
-   Updated risk.
-   Current portfolio context.

Entries are never opened because earlier entries were profitable.

------------------------------------------------------------------------

# Article 5 --- Scalping Campaigns

A scalp campaign is not a mode.

It is an opportunity whose expected holding horizon is short.

Example campaign:

Entry.

Profit.

Close.

Re-evaluate.

If opportunity remains:

Enter again.

Profit.

Close.

Re-evaluate.

Repeat until expected value falls below acceptance.

The number of trades is not predetermined.

The campaign ends when the opportunity ends.

------------------------------------------------------------------------

# Article 6 --- Swing and Intraday Campaigns

The same constitutional process applies to longer horizons.

Only the estimated holding horizon changes.

The Brain determines:

-   Continue.
-   Reduce.
-   Expand.
-   Protect.
-   Exit.

Duration is discovered through reasoning rather than configured
beforehand.

------------------------------------------------------------------------

# Article 7 --- Campaign Termination

A campaign terminates when:

-   Expected value deteriorates.
-   Contradictory evidence dominates.
-   Opportunity disappears.
-   Risk becomes unacceptable.
-   Original thesis is invalidated.

Termination is immediate.

No emotional or historical attachment exists.

------------------------------------------------------------------------

# Article 8 --- Campaign Closure

After closure the campaign is frozen as a historical artifact.

It becomes available for:

-   Audit.
-   Learning.
-   Similarity retrieval.
-   Adaptive updates.
-   Institutional memory.

No campaign is forgotten.

------------------------------------------------------------------------

# Constitutional Rule

Apex shall never think in isolated trades.

It shall think only in opportunities.

Trades are temporary expressions of an evolving market campaign.

The campaign begins with validated reasoning and ends only when
continued reasoning concludes that the opportunity no longer offers
positive expected value.


------------------------------------------------------------------------

# Part VI --- Execution & Position Management Constitution

> Version: Draft 1.0

## Purpose

This chapter defines how an approved campaign is translated into
deterministic execution. The execution layer is not a second trader. It
faithfully implements the AI Cognitive Brain's authorized campaign while
enforcing execution feasibility and safety constraints.

------------------------------------------------------------------------

# Article 1 --- Separation of Responsibilities

The AI Cognitive Brain determines:

-   Whether to trade.
-   Direction.
-   Campaign objectives.
-   Campaign evolution.
-   Campaign termination.

The execution layer determines only:

-   Whether execution is technically possible.
-   Whether broker constraints are satisfied.
-   Whether safety constraints are violated.

Execution shall never reinterpret market evidence.

------------------------------------------------------------------------

# Article 2 --- Campaign Translation

The Brain emits a campaign specification including:

-   Thesis.
-   Desired exposure.
-   Initial execution intent.
-   Risk rationale.
-   Invalidation conditions.
-   Campaign objectives.

Execution consumes this specification without altering its market
thesis.

------------------------------------------------------------------------

# Article 3 --- Execution Validation

Before any order is transmitted, the execution layer validates:

-   Broker connectivity.
-   Instrument availability.
-   Market session.
-   Margin sufficiency.
-   Position limits.
-   Duplicate order prevention.
-   Order syntax.
-   Price freshness.
-   Acceptable spread.
-   Acceptable slippage policy.

If validation fails, execution is rejected and the Brain is informed.

------------------------------------------------------------------------

# Article 4 --- Position Construction

Position construction is campaign-aware.

The Brain may authorize:

-   Initial entry.
-   Additional entries.
-   Partial reductions.
-   Full liquidation.
-   Protective adjustments.

Every new execution requires renewed authorization from the Brain.

------------------------------------------------------------------------

# Article 5 --- Position Management

During an active campaign the Brain continuously reassesses:

-   Thesis strength.
-   Opportunity quality.
-   Expected value.
-   Portfolio impact.

Management actions may include:

-   Hold.
-   Scale in.
-   Scale out.
-   Protect profit.
-   Tighten risk.
-   Exit.
-   Reverse.

Execution performs these actions exactly as authorized.

------------------------------------------------------------------------

# Article 6 --- Profit Protection

Profit protection is driven by campaign reasoning.

Protective actions should reflect:

-   Current opportunity quality.
-   Remaining expected value.
-   Downside risk.
-   Campaign objectives.

No profit protection rule should exist solely because it has been
historically configured.

------------------------------------------------------------------------

# Article 7 --- Failure Handling

Execution failures become observations.

Examples:

-   Rejected order.
-   Partial fill.
-   Excessive slippage.
-   Connection failure.

These events return to the AI Cognitive Brain as evidence for subsequent
reasoning.

------------------------------------------------------------------------

# Article 8 --- Traceability

Every execution event shall record:

-   Campaign identifier.
-   Decision identifier.
-   Validation outcome.
-   Broker response.
-   Timestamp.
-   Execution metrics.
-   Result.

Every action must be reconstructable.

------------------------------------------------------------------------

# Constitutional Rule

Execution exists to faithfully realize the AI Cognitive Brain's
decisions within deterministic operational constraints.

Execution shall never become a competing source of market intelligence
or trading judgment.


------------------------------------------------------------------------

# Part VII --- Post-Trade Intelligence, Learning & Institutional Memory

> Version: Draft 1.0

## Purpose

This chapter defines the constitutional lifecycle after a campaign ends.
Campaign closure is not the end of Apex's work. It is the beginning of
institutional learning.

------------------------------------------------------------------------

# Article 1 --- Campaign Reconstruction

Every completed campaign shall be reconstructed in full.

The reconstruction includes:

-   Original thesis.
-   Evidence available at every decision.
-   Entries and exits.
-   Management actions.
-   Execution quality.
-   Market evolution.
-   Final outcome.

Nothing is discarded.

------------------------------------------------------------------------

# Article 2 --- Decision Audit

The audit evaluates reasoning quality rather than financial outcome
alone.

Questions include:

-   Was the thesis logically consistent?
-   Was the evidence sufficient?
-   Were contradictory signals ignored?
-   Was execution faithful?
-   Did campaign management remain aligned with the thesis?

Winning through poor reasoning is not considered success.

Losing despite sound reasoning is not automatically considered failure.

------------------------------------------------------------------------

# Article 3 --- Counterfactual Analysis

For every campaign Apex should evaluate alternative paths.

Examples:

-   What if no trade had been taken?
-   What if scaling had stopped earlier?
-   What if the campaign had reversed?
-   What if profit protection had occurred later?

Counterfactuals generate hypotheses, not historical truth.

------------------------------------------------------------------------

# Article 4 --- Learning

Learning updates confidence in evidence sources.

It may:

-   Reward evidence that consistently improves decisions.
-   Reduce influence of misleading evidence.
-   Discover recurring market patterns.
-   Propose new hypotheses for validation.

Learning shall not directly rewrite production behaviour without the
system's defined validation process.

------------------------------------------------------------------------

# Article 5 --- Institutional Memory

Every campaign becomes institutional knowledge.

Stored information includes:

-   Campaign identifier.
-   Market state.
-   Evidence snapshot.
-   AI reasoning.
-   Confidence.
-   Uncertainty.
-   Decisions.
-   Outcomes.
-   Lessons.

Institutional memory exists for retrieval, comparison and future
reasoning.

------------------------------------------------------------------------

# Article 6 --- Explainability

Apex must be able to explain:

-   Why it traded.
-   Why it did not trade.
-   Why it continued.
-   Why it exited.
-   Why it changed its thesis.

Every conclusion should be traceable to recorded evidence.

------------------------------------------------------------------------

# Article 7 --- Continuous Improvement

Recurring weaknesses become engineering and research inputs.

Approved operational actions may include:

-   Creating documentation.
-   Recording research tasks.
-   Updating dashboards.
-   Producing reports.
-   Creating engineering issues.

Operational actions follow governance policies.

------------------------------------------------------------------------

# Constitutional Rule

Every completed campaign increases Apex's institutional knowledge.

No campaign is treated as merely profit or loss.

Every campaign is a structured learning opportunity that strengthens
future reasoning through evidence, audit and validated adaptation.


------------------------------------------------------------------------

# Part VIII --- Adaptive Intelligence Constitution

Defines how Apex evolves while preserving stability.

-   Adaptive weights are earned through validated performance.
-   Learning never bypasses governance.
-   Changes require statistical evidence.
-   Evidence influence may increase or decrease.
-   AI performance is continuously measured.
-   Hypotheses are promoted, demoted or retired.
-   Adaptation prioritizes long-term robustness over short-term profit.

**Constitutional Rule:** Evolution must improve reasoning quality, not
merely recent profitability.


------------------------------------------------------------------------

# Part IX --- Composio Operational Intelligence Constitution

> Version: Draft 2.0

## Purpose

Composio is Apex's Universal Operational Capability Layer. Composio is not an AI,
not a reasoning engine, not a trading engine, and not an execution engine. It
extends Apex beyond market reasoning by allowing the AI Cognitive Brain to safely
perceive, retrieve, create, update and coordinate information across external
systems. Its purpose is not to make Apex smarter — it is to allow Apex to *act
upon* its intelligence.

**Article 1 — The operational nervous system.** The Brain is cognition; Composio
is action. Brain → Decision → Nervous System → Muscles → Action. Without Composio
the Brain only understands; with Composio it can influence the outside world.

**Article 2 — The AI never thinks about APIs.** The AI shall never reason in
terms of APIs, HTTP requests, or software providers. It reasons only in terms of
objectives ("I need historical research", "I should notify the operator", "this
recurring engineering failure should become a GitHub issue"). The AI creates
objectives, never integrations.

**Article 3 — Composio is a capability layer.** Composio exposes capabilities,
not software or APIs: retrieve research / documents / datasets / historical
reports; create engineering tasks / documentation; send notifications; store
institutional knowledge; schedule work; monitor infrastructure; update
dashboards; generate reports; coordinate workflows. The AI selects objectives;
the Action Planner selects capabilities; Composio performs execution.

**Article 4 — Information flow.** The AI does not directly consume Composio; it
requests knowledge. AI → "I need macroeconomic context" → Action Planner →
Composio → source → raw information → Evidence Engine → structured Evidence →
Brain. Composio never injects conclusions; it only retrieves reality.

**Article 5 — Action flow.** When the AI decides an external action should occur:
Reasoning → Objective → Governance → Action Planner → Composio → External System
→ Observation → Memory → Learning. Every action becomes another learning
opportunity.

**Article 6 — No direct trading authority.** Composio shall never become part of
the trading decision: never Buy, Sell, Exit, Reverse, Campaign, Expected value,
or Market thesis. Composio has zero market intelligence. The AI Brain remains the
only cognitive authority.

**Article 7 — No broker authority.** Broker execution remains inside Apex's
deterministic execution architecture (AI Brain → Execution Validation →
Execution Engine → Broker). Composio shall never be the path through which broker
orders are placed, modified or cancelled, and remains completely outside it.

**Article 8 — Knowledge retrieval.** Composio lets the Brain retrieve knowledge
from external systems (economic calendars, research repositories, institutional
documents, GitHub, Google Drive, Notion, documentation, historical archives,
cloud storage, reports). Every retrieved item becomes structured Evidence; the
Brain never reasons over raw APIs.

**Article 9 — Operational intelligence.** After reasoning, the Brain may
determine that a recurring software failure requires investigation, a discovery
should become documentation, today's campaigns require a report, infrastructure
health should be checked, a retraining job should be scheduled, historical data
should be archived, research should be organised, or engineering should
investigate a behaviour. Composio performs these operational tasks.

**Article 10 — Institutional ecosystem.** The objective is not automation; it is
institutional intelligence. Every external system becomes an extension of Apex's
operational ecosystem: GitHub = engineering memory, Google Drive = research
storage, Notion = institutional documentation, Slack = operational coordination,
Telegram = immediate communication, Cloud = computational infrastructure,
Calendar = future planning. The AI sees capabilities, never applications.

**Article 11 — Action Planner.** Between the AI Brain and Composio exists the
Action Planner. Its responsibilities: interpret objectives; select the
appropriate capability; choose the appropriate provider; construct the execution
plan; submit to Governance; execute through Composio; observe the result; return
observations to Memory. The Brain never needs to know whether GitHub, Jira,
Notion or another provider performed the task — only the objective matters.

**Article 12 — Governance.** Every external action requires governance, which
evaluates permissions, operational risk, business policy, security, approval
requirements and audit requirements. Only approved objectives become executable
actions.

**Article 13 — Observation.** Every completed external action returns
observations (issue created, dataset archived, notification delivered, calendar
retrieved, infrastructure restarted, documentation updated). These observations
become institutional memory.

**Article 14 — Learning.** The AI evaluates whether an action achieved its
objective and improved operations / research / engineering, and whether similar
actions should recur. Operational behaviour continuously improves through
feedback.

**Article 15 — Constitutional rule.** Composio shall never become Apex's Brain,
trader, or broker. Composio is Apex's universal operational capability layer: the
AI Cognitive Brain reasons; the Action Planner transforms objectives into
capabilities; Composio executes.

**Rule:** Composio operates the ecosystem, not broker orders.


------------------------------------------------------------------------

# Part X --- Governance & Safety Constitution

Defines deterministic safety constraints.

-   Policy enforcement.
-   Approval workflows.
-   Operational permissions.
-   Risk ceilings.
-   Audit logging.
-   Execution feasibility.

**Rule:** Governance validates safety, not market direction.


------------------------------------------------------------------------

# Part XI --- Engineering Constitution

-   Architecture over shortcuts.
-   Modular design.
-   Explainability.
-   Testability.
-   Traceability.
-   Event-driven communication.
-   No hidden decision logic.

**Rule:** Every implementation must align with the Constitution.


------------------------------------------------------------------------

# Part XII --- Observability Constitution

Every subsystem must emit observable events.

-   Metrics.
-   Logs.
-   Traces.
-   Campaign lifecycle.
-   AI reasoning summaries.
-   Execution telemetry.

Nothing important occurs without observability.


------------------------------------------------------------------------

# Part XIII --- Validation & Testing Constitution

Required:

-   Unit tests.
-   Integration tests.
-   Simulation.
-   Walk-forward validation.
-   Paper trading.
-   Controlled production rollout.

Reasoning quality must be measurable.


------------------------------------------------------------------------

# Part XIV --- Acceptance Criteria

The redesign is complete only when:

-   Single Reasoner Principle holds.
-   Evidence modules never trade.
-   AI performs reasoning.
-   Execution never reinterprets market intent.
-   Campaigns are opportunity-based.
-   Learning is auditable.
-   Explainability is available.

Any violation requires redesign.


------------------------------------------------------------------------

# Part XV --- Future Evolution Constitution

Future development must preserve constitutional principles.

New capabilities must:

-   Integrate into the cognitive loop.
-   Preserve explainability.
-   Preserve governance.
-   Preserve auditability.
-   Strengthen evidence-driven reasoning.

The Constitution evolves deliberately, never accidentally.


------------------------------------------------------------------------

# Part XVI --- Hardcoding & Architectural Abstraction Constitution

> Version: Draft 1.0

## Purpose

This Constitution defines what is permitted to be hardcoded within Apex and what
must remain abstract, discoverable and replaceable. The objective is to ensure
Apex evolves independently of specific technologies, vendors, APIs, providers or
infrastructure. Architecture shall remain permanent; implementations shall remain
replaceable.

**Article 1 — The fundamental law.** Architecture is permanent; implementations
are temporary. Therefore architecture may be hardcoded; implementations shall
not.

**Article 2 — What may be hardcoded.** Only architectural concepts may be
permanently encoded: AI Cognitive Brain, Evidence Engine, Campaign Manager,
Memory, Learning Engine, Adaptive Layer, Action Planner, Governance, Execution
Engine, Execution Validation, Capability Registry, Institutional Memory,
Reasoning Pipeline, Opportunity Harvesting, Campaign Lifecycle. These
constitutional concepts define Apex itself.

**Article 3 — What shall never be hardcoded.** Applications, providers, vendors,
API endpoints, cloud vendors, database vendors, messaging platforms,
documentation platforms, storage providers, research providers, communication
providers. Examples (implementations, not architecture): GitHub, GitLab, Jira,
Linear, Slack, Discord, Telegram, WhatsApp, Google Drive, Dropbox, Notion,
Confluence, AWS, Azure, GCP, Cloudflare, OpenAI, Anthropic, Llama, DeepSeek,
Qwen, Mistral.

**Article 4 — The AI never knows applications.** The Brain shall never reason
about software. Not "Create a GitHub issue" but "Create an engineering task"; not
"Read Google Drive" but "Retrieve historical research"; not "Send Telegram
message" but "Notify the operator". The AI reasons only about objectives.

**Article 5 — Capabilities.** Every external integration shall expose
capabilities (Retrieve Document, Store Document, Notify Operator, Create
Engineering Task, Retrieve Market Context, Retrieve Calendar, Archive Research,
Generate Report, Monitor Infrastructure, Schedule Activity, Retrieve
Institutional Knowledge). The AI understands capabilities, never providers.

**Article 6 — The Capability Registry.** Apex maintains a Capability Registry
mapping a capability to its available providers (e.g. Notify Operator → Slack,
Telegram, Discord, Email; Create Engineering Task → GitHub, Jira, Linear). The
registry is dynamic; the AI never changes.

**Article 7 — Action Planner.** The Action Planner receives an objective,
searches the registry, determines available providers, evaluates availability,
permissions, reliability, latency, business policy and governance, chooses the
optimal provider, and executes. The Brain never performs provider selection.

**Article 8 — Provider replacement.** Replacing a provider shall never require
changing the AI. GitHub today, Jira tomorrow — the AI keeps reasoning "Create
Engineering Task"; only the Capability Registry changes.

**Article 9 — Model abstraction.** The Brain shall never depend on one language
model (OpenAI, Llama, DeepSeek, Anthropic, Qwen, Mistral, future models — all
implementations). The Brain reasons; the Model Manager selects the reasoning
model by policy, capability, latency, cost, availability and performance.

**Article 10 — Infrastructure abstraction.** Not "Launch AWS / Restart Kubernetes
/ Scale Docker" but "Increase computational resources / Restart reasoning
infrastructure / Recover failed worker". The Infrastructure Manager determines
the implementation.

**Article 11 — Communication abstraction.** Not "Send Slack / Telegram / Email"
but "Notify Operator". The Communication Manager chooses the implementation.

**Article 12 — Knowledge abstraction.** Not "Search Google Drive / Notion /
Confluence" but "Retrieve Institutional Knowledge". The Knowledge Manager
determines the source.

**Article 13 — Market data abstraction.** Not "Query Alpha Vantage / Polygon /
Broker API" but "Retrieve Market Context". The Market Data Manager determines the
source.

**Article 14 — Future extensibility.** New providers may be added, old ones
removed; providers may fail or become obsolete. None of these shall require
redesigning the Brain — only the Capability Registry and provider adapters
change.

**Article 15 — Constitutional rule.** The Brain reasons exclusively in
objectives; architecture reasons in capabilities; infrastructure reasons in
providers. Providers remain replaceable, capabilities remain stable, architecture
remains permanent. If replacing an external application requires modifying the AI
Cognitive Brain, the architecture has violated this Constitution and must be
redesigned.

**Rule:** Objectives are permanent; capabilities are stable; providers are
replaceable.

# Part XVII --- Multi-Model Reasoning Constitution

> Version: Draft 1.0

## Purpose

Defines how Apex interacts with multiple AI reasoning models while preserving the
Single Reasoner Principle. The objective is not multiple competing brains — it is
one Cognitive Brain capable of consulting multiple reasoning engines before
reaching a final conclusion.

**Article 1 — The Cognitive Brain.** There is only one cognitive authority: the
AI Cognitive Brain. It owns understanding, reasoning, hypothesis generation,
expected-value estimation, campaign creation/evolution/termination and learning.
No external model becomes a second brain.

**Article 2 — Reasoning engines.** Language models (OpenAI, Claude, Gemini,
Llama, DeepSeek, Qwen, Mistral, future models) are reasoning engines —
implementations that perform inference. They do not become Apex.

**Article 3 — Model independence.** Apex shall never depend on one reasoning
provider. The Brain is permanent; engines are replaceable; replacing a provider
never requires redesigning the Brain.

**Article 4 — Internal reasoning.** Apex may maintain preferred models via direct
API integrations or locally hosted models; these are the Brain's internal
reasoning infrastructure, available regardless of Composio.

**Article 5 — Composio AI capabilities.** Composio may expose additional
reasoning providers as *optional* resources. They extend the Brain's ability to
seek perspectives; they do not replace it. The Brain remains final authority.

**Article 6 — Consultative reasoning.** The Brain may consult one or more
engines: evidence → Brain constructs an initial thesis → requests specialist
analysis → engines respond → Brain evaluates and *challenges* every response →
Brain constructs the final thesis. External engines never directly create
campaigns.

**Article 7 — No majority voting.** The Brain shall never blindly follow the
majority. Three models agreeing does not make a thesis correct. Every external
opinion is *evidence, not truth*, evaluated on supporting/contradictory evidence,
historical reliability, current context and expected value.

**Article 8 — Specialist reasoning.** Engines have different strengths (deep
reasoning, code, pattern explanation, risk analysis, research, long-context).
Provider selection depends on capability, not preference.

**Article 9 — Reasoning Orchestrator.** Between the Brain and external engines
exists the Reasoning Orchestrator: it selects appropriate engine(s), manages
latency/cost/privacy/reliability, collects responses, and returns structured
reasoning to the Brain. The Brain never communicates directly with providers.

**Article 10 — Final decision authority.** External engines never determine buy,
sell, campaign, expected value, campaign termination or expansion. Only the Brain
has constitutional authority over market decisions.

**Article 11 — Continuous validation.** The Brain continuously measures each
engine's reasoning quality, contribution quality, historical usefulness, latency,
reliability and cost; future selection is informed by measured performance, not
assumption.

**Article 12 — Constitutional rule.** Apex possesses one Brain; it may consult
many engines; every engine is an advisor. The Brain alone synthesizes all
evidence, evaluates all external opinions, constructs the final thesis, authorizes
campaigns and remains the sole decision-maker. Multiple engines increase
perspective; they do not divide authority.

**Rule:** Many advisors, one Brain — opinions are evidence, never votes.

----------------------------------------------------------------------

# Part XVIII --- Live Campaign Management Constitution

> Version: 1.0

## Purpose

Defines how Apex manages every active market opportunity after execution.
Management is not a secondary subsystem, a collection of trading rules, stop-loss
movement or profit taking — management is **continuous reasoning**. The AI
Cognitive Brain shall continuously reason about every active campaign until the
campaign no longer exists. A trade begins a campaign; management determines its
outcome. Management is therefore one of the highest responsibilities of the Brain.

**Article 1 — The campaign is the unit of management.** Apex never manages
positions; it manages campaigns. A position is only one expression of a campaign,
which may hold one or many positions, partial exits, multiple entries, scaling,
re-entry, profit protection or complete liquidation. A campaign persists until
reasoning determines the opportunity has ended.

**Article 2 — The Brain never stops thinking.** Execution does not end reasoning;
it begins continuous reasoning. The Brain continuously asks what changed, what
stayed the same, whether the thesis strengthened or weakened, what evidence
appeared or disappeared, whether the campaign should evolve, whether exposure
should change, and whether it should do nothing — until termination.

**Article 3 — Every campaign is an independent cognitive process.** Every active
instrument owns an independent campaign with its own evidence, reasoning,
expected value, confidence, uncertainty, management, objectives and lifecycle.
Campaigns never share reasoning unless portfolio reasoning requires it.

**Article 4 — The global Cognitive Brain.** Although campaigns are independent,
there is only one Brain. It simultaneously reasons across every active campaign
and maintains campaign, portfolio, market, operational and institutional
intelligence. The Brain remains the single cognitive authority.

**Article 5 — Continuous reasoning.** Every meaningful market event triggers
reasoning (new candle, liquidity shift, momentum change, volatility expansion,
spread widening, order flow, news, correlation change, portfolio change,
execution failure). Every event becomes evidence; every evidence update may
change campaign behaviour.

**Article 6 — Position management.** The Brain may hold, increase, reduce, scale,
protect profit, re-enter, exit partially, exit completely, reverse or do nothing.
Every management action must originate from reasoning — never from predetermined
rules alone.

**Article 7 — Opportunity harvesting.** The objective is not position management
but opportunity harvesting. The Brain continuously asks whether additional value
can still be extracted; the campaign exists only while positive expected value
exists.

**Article 8 — Scalping campaigns.** A scalp campaign is rapid reasoning,
execution, re-evaluation, profit extraction and termination. Every completed
scalp immediately returns to reasoning: if the opportunity still exists, trade
again; if not, stop. Every scalp trade earns its own existence.

**Article 9 — Longer-horizon campaigns.** The same reasoning applies to intraday,
swing and multi-day campaigns. Only the expected holding horizon changes; the
management philosophy never changes.

**Article 10 — Consultative reasoning.** The Brain may request advisory analysis
from external engines (OpenAI, Claude, local Llama, DeepSeek, future engines).
These provide opinions, never decisions; every opinion becomes evidence the Brain
critiques before accepting. The Brain alone produces the campaign decision.

**Article 11 — Portfolio intelligence.** The Brain continuously reasons across the
whole portfolio: correlation, capital concentration, redistribution, whether one
campaign raises another's risk, and whether a campaign should terminate because a
higher expected-value opportunity exists. Portfolio reasoning interacts
continuously with campaign reasoning.

**Article 12 — Campaign health.** Every campaign continuously maintains its current
thesis, expected value, confidence, uncertainty, opportunity strength, evidence
quality, risk, remaining upside/downside and management objectives. Health is
recalculated continuously.

**Article 13 — Management never ends.** Management does not occur on a fixed
interval; it occurs whenever meaningful evidence changes. The Brain never waits
for arbitrary timers when important market information becomes available.
Reasoning follows reality, not clocks.

**Article 14 — Campaign termination.** Campaigns terminate only when reasoning
concludes expected value no longer justifies participation, the opportunity has
disappeared, contradictory evidence dominates, risk exceeds acceptable limits, or
the original thesis has failed. Termination is immediate.

**Article 15 — Post-termination.** Closure immediately begins audit, learning,
institutional memory, counterfactual analysis and adaptive learning. Every
completed campaign becomes another teacher.

**Article 16 — Constitutional rule.** The Brain remains responsible for every
active campaign from birth to death. Execution opens campaigns; the Brain manages
them; learning improves them; memory preserves them; external engines advise them;
portfolio intelligence coordinates them. No other subsystem shall independently
manage market opportunities. Excellent campaign management — not merely excellent
entries — is the primary determinant of Apex's long-term performance.

**Rule:** One Brain, continuous reasoning, every campaign, birth to death — no
other subsystem manages market opportunities in parallel.

----------------------------------------------------------------------

# Part XIX --- Opportunity Qualification & Cognitive Decision Constitution

> Version: 1.0

## Purpose

Governs the complete cognitive cycle before, during and after every market
decision — the constitutional law that determines whether Apex is permitted to
participate at all. This is not a trading strategy. Any subsystem that behaves
differently is architecturally incorrect regardless of technical performance.

**Fundamental principle.** The market produces infinite movement. Movement is
not opportunity; opportunity is not profitability; profitability is not win rate
— profitability is positive long-term expected value after all costs, uncertainty
and risk. Apex's responsibility is not to detect movement but to continuously
separate meaningful opportunity from market noise.

**Article 1 — The market is presumed noise.** Every tick, candle, breakout,
pullback and momentum burst is initially noise. No market event becomes an
opportunity until sufficient evidence proves otherwise. The burden of proof
belongs to reality, never to the AI.

**Article 2 — Observation.** The Brain continuously observes price, liquidity,
volume, order flow, structure, momentum, volatility, spread, execution quality,
correlation, macro events, portfolio state, institutional behaviour and
historical analogues. Every observation becomes structured evidence; nothing
becomes a decision at this stage.

**Article 3 — Evidence.** Every analytical subsystem contributes evidence only —
never buy/sell/close/reverse/increase/reduce. Signals do not exist; only evidence
exists, carrying confidence, reliability, recency, context, uncertainty, source
quality and historical performance.

**Article 4 — Multiple hypotheses.** The Brain never constructs only one
explanation. For every market state it generates multiple competing hypotheses
(continuation, pullback, liquidity sweep, reversal, compression, expansion,
accumulation, distribution, false breakout, exhaustion) and actively searches for
evidence that destroys each one.

**Article 5 — Multi-horizon reasoning.** The market exists across many horizons
simultaneously (30s → weekly); reasoning does too. Each horizon keeps its own
thesis, probability, uncertainty, expected value, opportunity score and holding
estimate. The Brain does not force agreement between horizons — it discovers
where opportunity currently exists.

**Article 6 — Opportunity discovery.** The Brain never asks "should I buy/sell?"
It asks whether an exploitable opportunity has emerged, where it is, how long it
may last, how uncertain it is, how much value remains, and what evidence supports
or rejects it — before qualification begins.

**Article 7 — Opportunity qualification.** Every opportunity must survive
qualification on expected gross profit, execution costs (spread, commission,
slippage, latency), risk, duration, success/failure probability, remaining
upside/downside, liquidity quality, portfolio interaction, confidence,
contradictory evidence, institutional context and adaptive uncertainty. **Only
opportunities with positive expected NET value may proceed.**

**Article 8 — Meta-reasoning.** The Brain reasons about its own reasoning: why it
currently believes a thesis, how reliable that reasoning has been historically,
whether it is reacting to noise or over-fitting, what evidence would change its
mind, whether it would still trade if costs doubled, whether waiting improves
decision quality, and whether it has become too aggressive or too conservative.
It critiques itself before it critiques the market.

**Article 9 — Consultative intelligence.** The Brain may consult additional
reasoning engines (OpenAI, Claude, Gemini, DeepSeek, Llama, future engines). Each
is an advisor, never a decision-maker; every opinion becomes evidence and is
challenged. The Brain alone owns the final decision.

**Article 10 — Campaign creation.** When qualification succeeds a campaign is
created representing the opportunity (not the trade). The opportunity determines
campaign size (one trade or a hundred) — never predefined rules.

**Article 11 — Execution.** Execution is merely the physical expression of
reasoning. It owns no intelligence, strategy or market understanding; it performs
the objective the Brain produced.

**Article 12 — Continuous management.** Execution begins continuous reasoning:
has opportunity strengthened or weakened, should exposure change, should another
trade harvest it, should the campaign terminate. Management never stops while the
campaign exists.

**Article 13 — Opportunity harvesting.** The objective is opportunity harvesting,
not position management. Opportunity remaining → harvest again; EV rising →
harvest more aggressively; EV falling → more conservatively; opportunity gone →
terminate immediately. The campaign exists only while positive EV exists.

**Article 14 — Adaptive trading frequency.** The Brain never targets a trade
count. Frequency emerges from qualified opportunities — zero, one, ten or a
hundred. Both overtrading and undertrading are prohibited.

**Article 15 — Self-regulation.** The Brain audits itself and automatically
increases selectivity when it detects excessive reversals, declining expectancy,
repeated false positives, high uncertainty, poor execution quality, overtrading
or noise-chasing — until statistical quality improves.

**Article 16 — Portfolio intelligence.** The Brain reasons across all active
campaigns — capital allocation, correlation, concentration, risk interaction,
opportunity ranking, capital efficiency, higher-value alternatives. Every
campaign competes for capital; capital belongs to the highest expected value.

**Article 17 — Termination.** Campaigns terminate immediately when expected value
disappears, the opportunity ends, risk dominates reward, contradictory evidence
overwhelms support, execution quality deteriorates, or capital is better deployed
elsewhere. Termination requires only reasoning, never emotion.

**Article 18 — Learning.** Every completed campaign enters institutional learning
— original thesis, evidence, alternative hypotheses, advisors consulted,
confidence, uncertainty, execution/management quality, outcome, counterfactuals,
opportunity and decision quality. The objective is learning whether the
*reasoning* was correct, not merely whether the trade won.

**Article 19 — Institutional memory.** Nothing is forgotten. Every opportunity,
campaign, hypothesis, consultation, execution, management action, audit and
lesson becomes institutional knowledge; future reasoning is built upon it.

**Final constitutional law.** The Brain shall continuously observe reality,
transform observations into evidence, construct and challenge competing
hypotheses, qualify opportunities through expected NET value, consult specialist
engines when beneficial, reason about its own reasoning, create campaigns only
when opportunities justify action, manage every campaign until opportunity
disappears, allocate capital to the highest-value opportunities, learn from every
outcome, preserve all knowledge, and repeat indefinitely. Apex shall never trade
every movement; it shall understand the market better than one cycle ago.

**Rule:** Presume noise; qualify on positive expected net value; the Brain owns
the decision — this cognitive cycle is the constitutional heartbeat and no
subsystem may bypass, weaken or compromise it.

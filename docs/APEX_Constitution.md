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

# Part XX --- Multi-AI Cognitive Intelligence Constitution

> Version: 1.0

## Purpose

Governs the integration of every Artificial Intelligence model within Apex — the
relationship between the Cognitive Brain, external reasoning engines, local
models, future models and every AI capability Apex will ever use. This is not an
implementation guide; it is constitutional law. Any AI integration that behaves
differently from this Part is architecturally incorrect.

**Fundamental principle.** Apex shall never depend upon a single Artificial
Intelligence. No single model is perfect; none possesses complete market
understanding. Every model has strengths and weaknesses. Institutional trading
firms consult multiple experts; Apex shall do the same.

**Article 1 — The Cognitive Brain.** The Apex Cognitive Brain is the permanent
intelligence of Apex. It owns reasoning, decision making, campaign management,
opportunity harvesting, learning, institutional memory, capital allocation,
governance and risk ownership. The Brain never delegates ownership — only
consultation.

**Article 2 — External AI models.** Every external AI model (OpenAI, Claude,
Gemini, Llama, DeepSeek, Qwen, Mistral, and future reasoning engines) exists as a
specialist advisor. These models never become the Brain; they advise the Brain.

**Article 3 — No model is privileged.** No model shall be permanently preferred
and no provider permanently trusted. Every model earns influence continuously
through evidence quality, historical accuracy, reasoning quality, domain
expertise, consistency and reliability. The Brain continuously evaluates every
advisor.

**Article 4 — Specialisation.** Different models possess different strengths —
macro reasoning, statistical reasoning, pattern recognition, counterarguments,
code generation, risk analysis, rapid inference. The Brain selects advisors
according to the reasoning task, not according to brand.

**Article 5 — Advisory Council.** The collection of AI models forms the Apex
Advisory Council, which exists to challenge thinking, not replace it. The Council
debates; the Brain concludes.

**Article 6 — Consultation.** The Brain may consult one advisor, several, or every
available advisor. The choice depends entirely upon opportunity complexity,
market uncertainty, required confidence, available computation and time
constraints. Consultation is adaptive, never fixed.

**Article 7 — Evidence.** Every advisor produces reasoning, probabilities,
counterarguments, alternative hypotheses, risk and opportunity observations,
confidence and uncertainty. Nothing an advisor produces becomes a decision;
everything becomes evidence.

**Article 8 — Disagreement.** Disagreement is desirable; perfect agreement may
indicate shallow reasoning. The Brain actively seeks disagreement because
contradictory reasoning expands understanding. Consensus is earned, never
assumed.

**Article 9 — Self-criticism.** The Brain shall continuously ask advisors why it
is wrong, what it has overlooked, what contradicts its thesis, why a campaign
should terminate and why it should avoid a trade. The purpose of consultation is
criticism, not validation.

**Article 10 — Dynamic provider management.** Providers shall never be hardcoded.
The AI ecosystem evolves continuously — models emerge and disappear; pricing,
performance and availability change. The Brain discovers available providers
dynamically; every provider is modular and replaceable; no architectural
dependency shall exist on any single vendor.

**Article 11 — Local and remote intelligence.** The Brain reasons using both
remote and local AI. Local reasoning protects continuity; remote reasoning
expands capability. Neither replaces the other; both cooperate.

**Article 12 — Cost awareness.** Reasoning is a resource. The Brain continuously
optimises latency, cost, quality, availability and reliability. Simple reasoning
may use inexpensive models; complex reasoning may justify premium models. The
objective is maximum intelligence per unit of computation.

**Article 13 — Failover.** If a provider becomes unavailable, exhausts credits or
produces unreliable analysis, reasoning shall continue. No campaign shall fail
because one AI provider failed.

**Article 14 — Continuous evaluation.** The Brain continuously evaluates advisors
on prediction quality, reasoning quality, counterargument quality, hallucination
frequency, decision contribution, latency, cost efficiency and historical
usefulness. Influence is earned continuously.

**Article 15 — Learning.** The Brain records which advisor was consulted, why, the
advisor's recommendation, the final decision, the outcome, the decision quality
and the advisor's contribution — so future consultations improve continuously.

**Article 16 — Composio.** Composio is neither the Brain nor an advisor; it is the
intelligence infrastructure providing secure access to AI providers,
applications, services, knowledge sources and operational tools. Composio expands
what the Brain can access; it never owns reasoning.

**Article 17 — The final authority.** Regardless of how many advisors, models or
providers participate, there shall always exist one final authority: the Apex
Cognitive Brain. The Brain alone integrates market evidence, advisor reasoning,
institutional memory, portfolio state, campaign intelligence, risk and expected
value. Only the Brain may authorise campaign creation, execution, scaling,
management, termination, capital allocation and learning. No external AI may
directly control trading behaviour.

**Final constitutional law.** Apex shall operate as an institutional cognitive
organisation rather than a single Artificial Intelligence. The Cognitive Brain
remains the permanent executive intelligence; every external AI model functions
as a specialist advisor that contributes evidence, challenges existing beliefs
and improves understanding. The Brain synthesises all available intelligence into
a unified market understanding, continuously evaluates the quality of every
advisor, dynamically selects the most appropriate reasoning engines for each
task, remains independent of any single provider, and alone accepts full
responsibility for every decision taken throughout the complete lifecycle of
every opportunity. No AI provider shall ever become Apex; all AI providers
collectively strengthen Apex; the Cognitive Brain alone is Apex.

# Part XXI --- The Institutional AI Ecosystem Constitution

> Version: 1.0

## Purpose

Defines the complete Artificial Intelligence ecosystem of Apex: every AI
provider, reasoning engine, local model, remote model, consultation, provider
evaluation, model activation and every future AI integration. This Constitution
is permanent; individual AI providers are temporary.

**Fundamental principle.** Apex shall never become dependent upon one Artificial
Intelligence. No provider, model, API or vendor owns Apex. The Cognitive Brain
owns intelligence; everything else exists to strengthen it. The objective is not
an AI-powered trading bot but an institutional cognitive organisation capable of
continuously improving through the collective intelligence of multiple reasoning
engines.

The complete ecosystem pipeline:

```
Market → Evidence Engine → Institutional Memory → AI Cognitive Brain
      → Reasoning Orchestrator → { Internal Models (Llama/Qwen/Local AI)
                                   | External Models (OpenAI/Claude/Gemini) }
      → Provider Manager → Capability Registry → Provider Health Monitor
      → Provider Evaluation Engine → Unified Advisory Intelligence
      → AI Cognitive Brain → Campaign Intelligence → Execution Architecture
      → Learning & Memory
```

**Article 1 — The Cognitive Brain.** There exists only one Brain, the Apex
Cognitive Brain. It owns understanding, reasoning, expected value, campaign
creation and management, opportunity harvesting, capital allocation, learning,
institutional memory and final authority. Nothing else owns decisions.

**Article 2 — The Reasoning Orchestrator.** The Brain never communicates directly
with AI providers. Every reasoning request enters the Reasoning Orchestrator,
which determines complexity, urgency, required confidence, latency, cost,
privacy and required expertise before any advisor is selected.

**Article 3 — The Advisory Council.** Every AI provider (OpenAI, Claude, Gemini,
DeepSeek, Llama, Qwen, Mistral and future engines) becomes part of Apex's
Advisory Council. They advise; they never decide.

**Article 4 — Provider Manager.** The Provider Manager maintains every reasoning
provider — authentication, configuration, health, availability, latency, rate
limits, cost, failover and lifecycle. No provider communicates directly with the
Brain.

**Article 5 — Provider states.** Every provider exists in one of three
constitutional states: **AVAILABLE** (configured, authenticated, healthy, ready);
**CONFIGURED** (integrated, architecture complete, awaiting credentials, able to
activate immediately); **UNAVAILABLE** (offline, disabled, failed, temporarily
excluded). Changing provider state shall never require architectural
modification.

**Article 6 — Capability abstraction.** The Brain never requests a named vendor
(OpenAI, Claude, Gemini); it requests a capability — strategic reasoning,
counterargument generation, risk analysis, engineering analysis, long-context
reasoning, rapid inference, code reasoning, pattern explanation, macro reasoning.
The Provider Manager selects the implementation.

**Article 7 — Provider selection.** Every reasoning request evaluates capability,
historical usefulness, latency, reliability, current availability, cost, privacy,
context length, provider health and reasoning quality. Selection is dynamic,
never static.

**Article 8 — Multi-model consultation.** Simple reasoning may consult one
advisor; complex reasoning may consult several; critical reasoning may consult
every available advisor. Consultation depth adapts automatically.

**Article 9 — Provider evaluation framework.** Every advisor is continuously
evaluated on reasoning quality, market understanding, counterargument quality,
hypothesis diversity, expected-value contribution, decision usefulness, campaign
contribution, historical usefulness, latency, reliability, cost efficiency,
failure frequency, hallucination frequency, constitutional compliance and
explainability. No advisor possesses permanent authority; authority is earned
continuously.

**Article 10 — Provider scorecard.** Every provider maintains a continuously
evolving score across domains — strategic, scalping, swing, risk, portfolio,
engineering, learning, research and operational reasoning. The Brain
continuously learns which advisors excel within each domain.

**Article 11 — Consultation records.** Every consultation records the reasoning
request, market state, campaign, providers consulted, responses, confidence,
counterarguments, the final Brain decision, the outcome and the decision quality.
These records become institutional memory.

**Article 12 — Self-improvement.** The Brain continuously asks which advisors
helped, which harmed, which were unnecessary and whether future consultations
should change. Advisor utilisation evolves continuously.

**Article 13 — Local intelligence.** Local reasoning engines exist for privacy,
offline operation, low latency, cost efficiency and business continuity. Local
reasoning remains permanently available.

**Article 14 — Remote intelligence.** Remote reasoning engines provide advanced
reasoning, massive context, specialised intelligence, research and alternative
viewpoints. They extend capability; they never replace cognition.

**Article 15 — Failover.** Provider failure shall never stop Apex. If OpenAI,
Claude or Gemini fails, reasoning continues; if all remote providers fail, local
reasoning continues and campaign intelligence remains operational.

**Article 16 — Future providers.** Future AI models require only a provider
adapter, capability mapping, configuration and validation — never a redesign of
the Cognitive Brain. The architecture remains permanent; providers remain
replaceable.

**Article 17 — Financial activation.** Every provider should be architecturally
integrated from the beginning; activation requires only API credentials,
permissions, configuration and budget. Financial growth enables providers;
financial growth never redesigns architecture.

**Article 18 — Constitutional law.** The Apex AI ecosystem shall operate as an
institutional council of specialist intelligences. The Cognitive Brain remains
the sole executive intelligence; the Reasoning Orchestrator coordinates
consultation; the Provider Manager manages provider lifecycle; the Capability
Registry abstracts provider implementation; the Provider Evaluation Framework
continuously measures the value of every advisor. The Brain learns which advisors
contribute the highest-quality reasoning for each cognitive domain. Every
provider remains replaceable, measurable, optional and subordinate to the
Cognitive Brain. The Cognitive Brain alone synthesises market evidence,
institutional memory, portfolio intelligence, campaign intelligence and advisory
reasoning into one unified market understanding. No provider shall ever become
Apex; every provider shall exist only to make Apex more intelligent than it was
during the previous reasoning cycle. This ecosystem shall evolve continuously
without architectural redesign, allowing Apex to improve indefinitely while
remaining independent of every individual AI vendor, model and technology.

# Part XXIII --- Complete Institutional AI Provider Architecture Constitution

> Version: 1.0

## Purpose

Defines the complete Artificial Intelligence provider ecosystem of Apex — every
reasoning provider, hosted model, local model, provider gateway, provider
manager, provider evaluation, provider lifecycle and future AI integration. This
document is permanent; providers are temporary; architecture is permanent.

**Fundamental principle.** Apex shall never be built around a vendor, an API, or
one LLM — it shall be built around intelligence. Providers may change, models may
disappear, companies may fail, pricing may change and new frontier models will
emerge; the architecture shall remain unchanged.

The complete AI ecosystem, organised into three provider tiers:

```
Market → Evidence Engine → Institutional Memory → AI Cognitive Brain
      → Reasoning Orchestrator → Provider Selection Engine
      → Provider Evaluation Engine → Capability Registry → Provider Manager
      → {
          TIER 1  Frontier Commercial : OpenAI, Anthropic Claude, Google
                    Gemini, xAI Grok, Cohere
          TIER 2  Hosted Open-Source  : Groq, OpenRouter, Together AI,
                    Fireworks AI, DeepInfra, Cerebras, Hugging Face
          TIER 3  Local Institutional : Llama, Qwen, DeepSeek, Mistral,
                    Gemma, Phi
        }
      → Unified Advisory Intelligence → AI Cognitive Brain
      → Campaign Intelligence → Execution Engine → Learning & Memory
```

**Article 1 — The Cognitive Brain.** The Cognitive Brain remains the only
permanent intelligence. It owns reasoning, market understanding, campaign
creation and management, opportunity harvesting, capital allocation, learning,
institutional memory, expected value and final authority. Nothing else owns
decisions.

**Article 2 — The Reasoning Orchestrator.** Every reasoning request enters the
Reasoning Orchestrator, which evaluates complexity, urgency, confidence
requirements, latency, cost, privacy, historical advisor usefulness, required
expertise, portfolio impact and market uncertainty. Only after evaluation are
advisors selected.

**Article 3 — The Provider Manager.** The Provider Manager maintains every
provider — authentication, configuration, credential management, API lifecycle,
health, availability, rate limits, cost, latency, failover, activation,
deactivation and version management. No provider communicates directly with the
Brain.

**Article 4 — The Capability Registry.** The Brain never requests providers; it
requests capabilities (strategic, risk, portfolio, counterargument, engineering,
research, pattern-explanation, macro, code and institutional reasoning). The
Capability Registry maps each capability to the providers that can serve it.

**Article 5 — Tier 1 (frontier commercial).** OpenAI, Anthropic Claude, Google
Gemini, xAI Grok and Cohere provide the highest-quality institutional,
long-context, executive, strategic, alternative and knowledge reasoning. They
advise; they never decide.

**Article 6 — Tier 2 (hosted open-source).** Groq, OpenRouter, Together AI,
Fireworks AI, DeepInfra, Cerebras and Hugging Face provide fast, low-cost,
scalable, alternative and fallback reasoning over hosted OSS models, plus
research.

**Article 7 — Tier 3 (local institutional).** Llama, Qwen, DeepSeek, Mistral,
Gemma and Phi provide offline reasoning, business continuity, privacy, low
latency, zero API dependency and permanently owned local intelligence. Tier 3
remains permanently available.

**Article 8 — Provider states.** Every provider exists in one of three states:
**AVAILABLE** (authenticated, healthy, ready), **CONFIGURED** (integrated,
architecture complete, awaiting credentials) and **UNAVAILABLE** (offline,
disabled, failed). Provider state changes require configuration only, never
redesign.

**Article 9 — Multi-model consultation.** Simple reasoning consults one advisor,
moderate reasoning several, critical reasoning the entire Advisory Council. The
consultation depth adapts dynamically.

**Article 10 — Advisory Council.** Every AI provider belongs to the Institutional
Advisory Council. The Council debates; the Brain concludes. No advisor owns
truth; every advisor contributes evidence, counterarguments, alternative
hypotheses, reasoning, probability, confidence and uncertainty.

**Article 11 — Provider evaluation framework.** Every provider is continuously
evaluated on reasoning quality, market understanding, opportunity and
expected-value contribution, counterargument quality, hypothesis diversity,
campaign/portfolio/engineering/research contribution, latency, availability,
reliability, cost efficiency, hallucination frequency, consistency,
constitutional compliance, explainability and decision contribution.

**Article 12 — Provider scorecards.** Each provider maintains independent
per-domain scores — executive, scalping, swing, campaign management, portfolio
management, risk, market, engineering, code, institutional learning, operational
intelligence and research. The Brain continuously learns which providers perform
best for each domain.

**Article 13 — Self-improvement.** Every consultation is recorded. The Brain
evaluates whether a provider improved reasoning, created unnecessary complexity,
increased confidence, discovered hidden risks or improved expected value. Future
provider selection evolves continuously.

**Article 14 — Failover.** Failure of one provider shall never stop Apex: a Tier 1
failure is replaced by Tier 2, a Tier 2 failure by Tier 3, and a remote-
infrastructure failure by local reasoning. Campaign intelligence never stops.

**Article 15 — Financial activation.** Every provider shall be architecturally
integrated immediately; activation requires only API credentials, permissions,
configuration and operational budget — no redesign, refactoring or architectural
modification. Financial growth activates providers; it never redesigns Apex.

**Article 16 — Future expansion.** Future providers require only a provider
adapter, capability mapping, credential configuration and validation — nothing
else. The Cognitive Brain remains unchanged.

**Final constitutional law.** The Apex AI ecosystem shall operate as a permanent
institutional council of specialist intelligences: Tier 1 frontier commercial,
Tier 2 scalable hosted open-source, Tier 3 permanently owned local. The Reasoning
Orchestrator coordinates consultation; the Provider Manager manages provider
lifecycle; the Capability Registry abstracts every provider behind stable
reasoning capabilities; the Provider Evaluation Framework continuously measures,
ranks and improves every advisor; and the Brain continuously learns which
providers contribute the greatest value within every cognitive domain. Every
provider remains replaceable, measurable, optional and subordinate to the
Cognitive Brain. The architecture shall be designed for every provider from the
first day regardless of whether credentials currently exist, and as financial
resources increase providers shall transition from CONFIGURED to AVAILABLE
without any architectural redesign. Every provider exists solely to increase the
intelligence of the Brain, strengthen opportunity harvesting, improve campaign
management, enhance institutional learning and maximise long-term positive
expected value. This Constitution is permanent. No provider shall ever become
Apex; only the Cognitive Brain is Apex.

# Part XXIV --- Universal Advisory Council Constitution

> Version: 1.0

## Purpose

Defines how every Artificial Intelligence integrated into Apex participates in
the Cognitive Ecosystem.

**Fundamental principle.** Every integrated Artificial Intelligence shall be
capable of becoming an advisor. No advisor becomes the Brain; no advisor
possesses executive authority. The Cognitive Brain remains the sole
constitutional intelligence and owns every market decision; the advisors
strengthen the Brain.

**Article 1 — The Universal Advisory Council.** Every integrated AI provider
becomes a permanent member of Apex's Universal Advisory Council — OpenAI,
Anthropic Claude, Google Gemini, xAI Grok, Cohere, Groq, OpenRouter, Together
AI, Fireworks AI, DeepInfra, Cerebras, Hugging Face, NVIDIA NIM, every local
reasoning engine (Llama, Qwen, DeepSeek, Mistral, Gemma, Phi) and every future
provider. Every member is architecturally equal; no provider receives permanent
preference.

**Article 2 — Advisor availability.** Every configured provider shall be capable
of participating in reasoning, existing in one of three operational states:
**AVAILABLE** (ready for consultation), **CONFIGURED** (integrated, awaiting
credentials or activation) and **UNAVAILABLE** (temporarily offline). Moving
between states requires only configuration; the Cognitive Brain remains
unchanged.

**Article 3 — Consultation.** The Brain determines whether consultation is
required: a simple situation may need no external advisor, a moderately complex
one several, and a highly uncertain or strategically important one many or all
available advisors. Consultation depth adapts dynamically.

**Article 4 — Specialisation.** Every advisor has different strengths (strategic
reasoning, counterargument generation, risk analysis, portfolio reasoning,
pattern recognition, code and engineering reasoning, research, long-context
reasoning, rapid inference, multimodal reasoning). The Brain continuously learns
them; provider selection shall always be capability-driven, never brand-driven.

**Article 5 — Provider evaluation.** Every advisor is continuously evaluated on
reasoning quality, market understanding, campaign contribution, opportunity
qualification, counterargument quality, portfolio contribution, historical
usefulness, latency, reliability, availability, cost efficiency, explainability,
hallucination frequency and constitutional compliance. Influence evolves with
measured performance.

**Article 6 — Dynamic advisory weighting.** No advisor possesses fixed authority;
influence is earned continuously. Providers that consistently improve reasoning
gain influence; those that reduce reasoning quality lose it. The Brain
continuously recalibrates advisor weighting from empirical evidence.

**Article 7 — Executive authority.** Regardless of how many advisors participate,
only the Apex Cognitive Brain may construct the final market thesis, determine
expected value, authorise/manage/terminate campaigns, allocate capital, scale
positions and learn from outcomes. Advisors provide intelligence; the Brain
makes decisions.

**Final constitutional law.** Apex shall operate as a permanent institutional
council of specialist intelligences. Every configured AI provider shall be
capable of acting as an advisor; every available provider shall be eligible for
consultation; every consultation shall be selected dynamically according to
capability, historical performance, latency, availability, cost and market
context. No provider shall ever become indispensable; no provider shall ever
become Apex. The Universal Advisory Council exists solely to strengthen the
reasoning of the Apex Cognitive Brain, which alone remains the permanent
executive intelligence responsible for every decision throughout the complete
market opportunity lifecycle.

# Part XXV --- Raw-Market Reasoning, Opportunity Discovery & Non-Collapsed Cognition Constitution

> Version: 1.0

## Purpose

Governs *how information reaches the Cognitive Brain and how the Brain's
cognitive state is represented*. Apex must not be a collection of indicators
voting LONG/SHORT/FLAT with a language model bolted on to arbitrate the votes.
Apex must observe market reality, understand it, generate competing
explanations, challenge them, estimate uncertainty and expected value, discover
exploitable opportunities, act only when justified, manage continuously, and
learn. Direction is a **consequence** of cognition, never its container.

**Fundamental principle.** The Brain shall receive the broadest available
*structured representation of market reality* and shall form its own hypotheses.
No subsystem may collapse observations into a directional vote before the Brain
has reasoned. `LONG`, `SHORT` and `FLAT` are execution consequences, not the
cognitive state of Apex.

**Article 1 — Observe → Understand → Interpret → Hypothesise → Challenge →
Estimate uncertainty → Discover opportunity → Estimate expected value → Assess
execution → Act → Manage → Re-evaluate → Exit/Continue/Reduce/Add/Re-enter →
Post-trade → Memory → Learning.** This is the mandatory cognitive pipeline. The
forbidden pipeline is `raw data → indicators → LONG/SHORT votes → aggregation →
LLM → LONG/SHORT/FLAT`.

**Article 2 — Evidence is not a vote.** Analytical modules (structure,
liquidity, momentum, volatility, volume, order flow, VWAP, FVGs, order blocks,
currency strength, correlation, higher/lower-timeframe behaviour, session,
execution quality) are *measurement instruments*. They may report observations
and measurements; they must never emit LONG/SHORT/NEUTRAL, and their outputs
must never be summed into a directional consensus. A thermometer reports
`38.4°C`, not `BUY`. Forbidden: `structure → LONG`; `5 LONG / 3 SHORT → LONG`.
Required: `structure: D1 higher-highs=true, M5 displacement=weak`;
`momentum: value=+0.37, acceleration=declining, divergence=present`.

**Article 3 — No precomputed directional consensus as primary cognition.** The
Brain must never be asked, as its primary method of market reasoning, to choose
between precomputed LONG/SHORT opinions. Any hidden transformation that
reintroduces directional voting anywhere in the pipeline is unconstitutional.

**Article 4 — Non-collapsed cognitive state.** A state such as
`{"direction":"LONG","confidence":0.55}` is too impoverished: a single
undefined confidence number conflates probability of price movement, thesis
confidence, opportunity confidence, entry-timing confidence, execution
confidence and hold confidence — different quantities. The Brain's internal
representation shall be richer than the final order instruction and shall
preserve, where meaningful: current regime/condition; a primary hypothesis;
alternative and third hypotheses; supporting and contradicting evidence; key
uncertainty; missing information; invalidation; the opportunity and its horizon;
expected favourable and adverse excursion; expected value; execution quality;
risk; portfolio interaction; what-would-change-my-mind; and only then a
recommended action.

**Article 5 — Discovery of unprogrammed opportunity.** Apex must be able to
recognise a valid opportunity no single analytical module predicted, including
opportunities against the higher-timeframe direction (a positive-EV short inside
a bullish market; a positive-EV long during a bearish lower-timeframe pullback
that is really a liquidity sweep being absorbed). Engineers must not predefine
every combination; the Brain is permitted to discover causal combinations.

**Article 6 — Self-criticism.** The Brain must challenge its primary explanation:
Why might I be wrong? What contradicts this? What alternative fits? What am I
missing? Am I anchoring to a timeframe? Am I confusing movement with
opportunity? Do costs destroy the edge? Has the opportunity already gone? What
would change my mind?

**Article 7 — Full market state.** Where technically and economically practical,
the Brain shall retain access to ticks, bid/ask, spread, tick and real volume,
candles across timeframes, depth/order-book where available, tick history,
volatility, price path, structure/liquidity/momentum/order-flow measurements,
correlation, session, regime, scheduled events, positions, account state,
available risk, execution conditions, historical and prior-campaign state and
institutional memory. More data is not more intelligence — but information about
timing, magnitude, relationships, uncertainty, contradiction and context must
survive to the Brain rather than being compressed to a direction beforehand.

**Article 8 — Movement is not opportunity.** `OPPORTUNITY = executable positive
expected value after risk, costs, latency, liquidity and uncertainty.` Strong
directional evidence with poor execution economics is NO TRADE; no clear
direction with a positive-EV, executable, well-defined event is a valid trade.
Opportunity horizon is set by the market (microseconds to weeks), never by a
predefined trading style. A theoretical opportunity Apex cannot observe and
execute in time is not an Apex opportunity.

**Article 9 — Advisors reason, they do not vote.** Every advisor receives the
same rich market state and reasons independently (thesis, counterargument,
alternative hypothesis). The Brain synthesises their *arguments*; it must never
merely count advisor votes. Disagreement is useful. An advisor reduced to an
indicator vote is unconstitutional.

**Article 10 — Valid cognitive outcomes.** The Brain may say: "I don't know";
"two explanations remain plausible"; "evidence is insufficient"; "the obvious
trend read is wrong"; "the higher timeframe is irrelevant to this opportunity";
"this is noise"; "short despite a bullish HTF"; "long despite bearish LTF"; "an
opportunity exists but costs make it unprofitable"; "no trade despite strong
directional evidence"; "keep observing." Uncertainty is acceptable; false
certainty is not. There shall be no automatic "FLAT because evidence conflicts,"
no automatic "LONG because trend is bullish," no automatic "SHORT because the
lower timeframe is bearish."

**Article 11 — Management uses the same cognition.** An open position is never
`LONG +0.55`; it is continuously re-reasoned (thesis validity, new/contradictory
evidence, market state, liquidity, volatility, execution, unrealised P&L,
exposure, opportunity cost, alternatives) → hold/reduce/protect/add/exit. A
position must not merely wait for TP or SL, and its direction must never become
confirmation bias. The Brain must be able to change its mind, exit before TP/SL
on thesis deterioration, and hold through thesis-consistent adverse movement.

**Article 12 — Execution is a deterministic consequence.** BUY/SELL/CLOSE/
REDUCE/ADD/HOLD are emitted only *after* cognition and remain a deterministic
control plane separate from reasoning. No execution decision may precede
cognitive assessment.

**Article 13 — No forced strategy or horizon.** Trend, scalping, swing,
mean-reversion, breakout, reversal, momentum, liquidity, order-block, FVG and
VWAP are observations or hypotheses, never cognitive prisons. No predefined
strategy may own the market; no predefined trade count or holding time may be
assumed. Every new entry earns its own justification; a prior profit never
authorises the next trade and a prior loss never mandates a hold.

**Article 14 — Live-code verification.** Compliance is judged against the live
codebase, not READMEs, diagrams, comments, module names or claimed function.
Auditors trace market data → normalisation → evidence → cognitive input →
AI/advisors → thesis → opportunity → risk → execution → management → exit →
post-trade → memory → learning, and must find and remove any hidden
transformation that collapses information into direction.

## Absolute non-negotiables

No indicator voting. No precomputed directional consensus as primary cognition.
No premature LONG/SHORT/FLAT compression. No forced strategy. No forced time
horizon. No automatic FLAT-on-conflict. No automatic LONG-because-trend. No
automatic SHORT-because-LTF. No assumption all opportunities look alike. No
automatic re-entry after a profitable trade. No automatic holding after a losing
trade. No single undefined confidence number as a substitute for reasoning. No
advisor reduced to an indicator vote. No execution decision before cognition. No
claim of compliance without live-code verification. No compromise on the
cognitive architecture.

## Current conformance — live-code audit (Version 1.0, honest baseline)

This Part is ratified as binding law. Roadmap items 1–3 (evidence de-collapse,
enriched cognitive state, prompt reframe) are **implemented and offline-verified**;
items 4–6 remain open. This section is the authoritative remediation backlog and
is updated as each item lands. Ratification does not assert full compliance.

- **RESOLVED (item 1) — evidence de-collapse.**
  `cognition/evidence_adapters.py` no longer emits `"{module} votes {DIR}"` or
  `"{module} supports/opposes {dominant}"`. Each module is surfaced as an
  *instrument reading* — a neutral observation carrying a signed, bounded
  "measured lean" and its secondary measurements (`evidence_from_votes`,
  `evidence_from_thesis_status`, `evidence_from_developing_bias`). The numeric
  `polarity`/`_sign` value is retained but re-scoped: it feeds only the
  conflict/uncertainty summary and post-hoc supporting/contradicting grouping —
  never a headline vote (Articles 2/3).
- **RESOLVED (item 3) — arbitration framing removed from the reasoner prompt.**
  `llm/reasoner.py::_SYSTEM_PROMPT` now frames the input as "a STRUCTURED
  REPRESENTATION OF MARKET REALITY — NOT a set of votes to arbitrate," demands
  observe→interpret→hypothesise→self-criticise→discover-opportunity, enforces
  movement≠opportunity and symmetric LONG/SHORT, and states direction is the
  consequence of reasoning (Articles 1/2/3/5/6/8).
- **RESOLVED (item 2) — non-collapsed cognitive state.**
  `llm/reasoner.py::LLMOpinion` now carries regime, primary/alternative
  hypotheses, supporting/contradicting evidence, key uncertainty, invalidation,
  opportunity + horizon, expected favourable/adverse excursion, expected value,
  execution quality, risk and what-would-change-my-mind. `direction`/
  `confidence` are derived as the execution consequence; parsing is
  backward-compatible (a legacy minimal reply still works). `cognition/brain.py`
  carries the rich state into the `DecisionPackage`/`CampaignSpec` (Article 4).
- **CONFORMS (already correct, must be preserved).** Non-directional risk-,
  advisor- and knowledge-context adapters are emitted as context "never a vote";
  the Brain is the sole decider; execution is a separate deterministic plane
  (Part XVIII/XIX); advisors already reason independently over shared state
  (Part XXIV). Movement-vs-opportunity net-EV qualification exists (Part XIX
  Art 7) and must be strengthened, not removed.
- **OPEN (items 4–6).** Advisor-independence review (Article 9), management-
  cognition parity (Article 11), and a live-code audit gate that fails on any
  reintroduced directional-vote collapse (Article 14).

## Remediation roadmap (✓ = landed)

1. ✓ **De-collapse evidence.** Instrument-reading observations (signed measured
   lean + measurements); polarity re-scoped to conflict/uncertainty only.
2. ✓ **Enrich the cognitive state.** `LLMOpinion` + `_SYSTEM_PROMPT` carry the
   Article 4 schema; direction/confidence derived last; backward-compatible
   parsing; rich state flows into the `DecisionPackage`/`CampaignSpec`.
3. ✓ **Reframe the prompt** to raw-state interpretation and opportunity
   discovery (Articles 1/5/6/8).
4. **Advisor independence review** — confirm advisors receive the same rich
   state and are synthesised as arguments, not counted (Article 9).
5. **Management cognition parity** — ensure open positions are re-reasoned with
   the same non-collapsed state (Article 11).
6. **Live-code audit gate** — extend the competing-decision-code audit to fail
   on any reintroduced directional-vote collapse (Article 14).

## Final law

Every subsystem, module, model, prompt, data transformation, adapter, strategy
component, risk mechanism, execution component, management component, learning
mechanism, optimisation, integration, refactor and line of code must answer:
does this preserve Apex's ability to independently understand the market,
discover opportunities that were not explicitly programmed, challenge its own
reasoning, and exploit genuinely positive-EV opportunities? If it destroys
information, forces predefined directional thinking, converts observations into
votes, prevents novel hypotheses, prevents independent advisor reasoning, or
causes premature LONG/SHORT/FLAT compression — reject or redesign it. No
compromise.

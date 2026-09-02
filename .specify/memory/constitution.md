<!--
Sync Impact Report
Version change: (unversioned template) → 1.0.0
Bump rationale: Initial ratification. The prior file was an unfilled scaffold with no
  adopted content, so this is the first governing version rather than an amendment.
Modified principles: none (all ten are new)
Added sections:
  - Core Principles I–X
  - Operating Constraints
  - Delivery Workflow
  - Governance
Removed sections: none (placeholder sections SECTION_2/SECTION_3 were named and filled)
Deferred TODOs: none
-->

# Multilingual Voice Billing Agent Constitution

Scope: an inbound voice agent for a fictional Swiss B2B company, handling invoice, payment,
credit, and billing-dispute calls in multiple languages. Built as a ~20-hour take-home for an
ElevenLabs Forward-Deployed Engineer role and demonstrated in a 3–5 minute Loom.

## Core Principles

### I. Verification Before Disclosure

No invoice, payment, balance, credit, dispute, or other financial information MUST be disclosed
before identity verification succeeds. The verification outcome MUST be decided server-side and
returned to the agent as a status; the LLM MUST NOT decide on its own that a caller is verified,
and MUST NOT be persuaded into disclosure by caller assertions.

Rationale: prompt-level gating is bypassable. Only a server decision is auditable and consistent
across languages, retries, and adversarial callers.

### II. Server-Side Authority for Financial Truth

Financial business rules, authorization, ownership checks, abuse checks, and data integrity MUST
be enforced in the backend independently of the LLM. The LLM MAY recommend an action; it MUST NOT
mutate authoritative financial or identity records on its own confidence. Every mutating tool MUST
re-validate its preconditions server-side even when the agent claims they already hold.

Rationale: the model is an interface, not a system of record. Correctness must survive a wrong,
confused, or manipulated model turn.

### III. Never Invent State

The agent MUST NOT fabricate payment status, credit eligibility, invoice ownership, or balances.
`UNKNOWN` and `SERVICE_UNAVAILABLE` MUST NEVER be reported to the caller as success. A temporary
failure MUST be communicated as distinct from an authoritative negative answer, and MUST route to
retry or escalation rather than to a guess.

Rationale: a confidently wrong balance or "your payment went through" is the highest-cost failure
mode in billing, worse than admitting the system cannot answer right now.

### IV. Data Minimization and Isolation

Sensitive identity data and authoritative financial data MUST live only in the secure backend.
They MUST NOT be replicated into the CRM, logs, or conversation summaries unless strictly required
for a stated purpose. Tools MUST return structured statuses (`VERIFIED`, `PARTIALLY_VERIFIED`,
`FAILED`, `LOCKED`) rather than raw sensitive fields. All data used in this project MUST be
synthetic.

Rationale: the blast radius of a leak is set by how far the data was copied. Statuses carry the
decision without carrying the secret.

### V. Escalation Is a First-Class Workflow

Any uncertainty, policy limit, risk signal, unrecoverable error, or explicit caller request MUST
route to a human. Handoff MUST be structured so the caller does not repeat themselves: verified
identity state, intent, entities gathered, actions attempted, and the escalation reason travel
with the transfer. A failed transfer MUST NOT abandon the caller; a fallback path (callback,
ticket, voicemail with reference) MUST be taken and confirmed aloud.

Rationale: escalation is the designed outcome for a large share of real billing calls, not an
error branch bolted on at the end.

### VI. Idempotency and Bounded Retries

Every mutating operation MUST carry an idempotency key. Retries MUST be bounded, with backoff and
explicit timeouts. Non-idempotent financial actions MUST NOT be blindly retried. Duplicate
webhooks MUST NOT create duplicate records.

Rationale: voice calls drop, tools time out, and callers repeat themselves. Without keys, a retry
becomes a second payment or a second credit.

### VII. Auditability

Every consequential automated action MUST record: action, previous state, new state, the
authorizing rule, relevant IDs (customer, invoice/payment/credit, conversation), timestamp, agent
version, risk-check result, and whether human approval was required.

Rationale: in a financial context, an action that cannot be reconstructed afterwards cannot be
defended, corrected, or trusted.

### VIII. Configurable Policy, Not Hard-Coded Thresholds

Credit limits, abuse windows, required verification factor counts, confidence thresholds, and
retention periods MUST be configuration, not literals in prompts or code paths. Changing a policy
value MUST NOT require editing agent instructions or redeploying business logic.

Rationale: these are the values a real customer would want to tune on day one, and the demo should
show that they are a dial rather than a rewrite.

### IX. Scope Discipline

Work MUST follow this priority order: (1) working golden path, (2) secure identity verification,
(3) correct financial behavior, (4) safe escalation, (5) tool reliability, (6) multilingual
experience, (7) auditability, (8) persistence/memory, (9) observability, (10) infrastructure
reproducibility, then extras. Nothing MUST be added merely to look sophisticated. A lower-priority
item MUST NOT be started while a higher-priority one is incomplete or broken.

Rationale: the budget is ~20 hours and the demo is 3–5 minutes. A narrow system that works beats a
broad one that does not.

### X. Security Baseline

Access MUST be least-privilege. Sensitive stores MUST be encrypted at rest. Secrets MUST live in a
secrets manager, MUST NOT be committed to the repository, and MUST NOT appear in logs alongside
raw identity fields. Tool endpoints MUST be authenticated. All tool input MUST be validated
server-side.

Rationale: this is the minimum bar at which a prototype is credible as the seed of a production
system.

## Operating Constraints

- **Languages**: the agent MUST detect and hold the caller's language across the whole call,
  including tool results, disclosures, and escalation messaging. Financial amounts, dates, and
  reference numbers MUST be rendered per locale conventions and MUST remain exact.
- **Swiss B2B context**: entities are companies, not consumers. Callers MUST be checked for
  authority to act on the account, not only for identity.
- **Latency**: tool calls on the golden path MUST have timeouts short enough to keep conversational
  turn-taking natural; a slow tool MUST produce a spoken holding turn rather than dead air.
- **Synthetic data only**: no real company, customer, invoice, or payment data enters the repository
  or any deployed environment.
- **Statuses over payloads**: tool contracts MUST expose enumerated outcomes; adding a raw sensitive
  field to a tool response requires an explicit justification recorded in the spec.

## Delivery Workflow

- Each feature MUST pass through `/speckit-specify` → `/speckit-plan` → `/speckit-tasks` before
  implementation, so scope stays inside Principle IX's priority order.
- Every change MUST state which principles it touches, and any deviation MUST be recorded with its
  justification and the cheaper alternative that was rejected.
- The golden path MUST be demonstrable end-to-end at every commit that claims a milestone; a broken
  golden path is a stop-the-line condition outranking all in-flight work.
- Failure paths that the demo asserts (verification failure, lockout, tool outage, escalation,
  transfer failure) MUST be exercised at least once against the real backend, not just reasoned
  about.
- The Loom MUST show the golden path plus at least one failure path, within 3–5 minutes.

## Governance

This constitution supersedes other practices and conventions in this repository. Where a
convenience conflicts with a principle, the principle wins.

**Amendments** MUST be made by editing this file, with a Sync Impact Report at the top recording
the version change, what moved, and any deferred items. **Versioning** is semantic: MAJOR for a
removed or redefined principle, MINOR for a new principle or materially expanded section, PATCH for
clarifications and wording.

**Compliance review**: every plan and implementation review MUST check the change against
Principles I–X. Principles I, II, III, and X are non-negotiable — a change that violates them is
rejected rather than justified. Violations of the remaining principles MUST be either fixed or
recorded as explicit, time-boxed debt in the feature's spec.

**Version**: 1.0.0 | **Ratified**: 2026-09-02 | **Last Amended**: 2026-09-02

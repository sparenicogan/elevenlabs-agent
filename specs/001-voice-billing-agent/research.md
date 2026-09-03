# Phase 0 Research: Multilingual Voice Billing Agent

**Date**: 2026-09-02 | **Spec**: [spec.md](./spec.md)

Most of the stack was fixed by the feature request. This document records the decisions that were
still open, the ones that were made *against* an obvious alternative, and the risks that need
verifying during implementation rather than after.

## D1. Tool surface consolidation

**Decision**: Six server tools plus two ElevenLabs system tools.

| Tool | Purpose |
|------|---------|
| `verify_identity` | Evaluate caller-supplied factors, return VERIFIED / PARTIALLY_VERIFIED / FAILED / LOCKED |
| `get_account_context` | Post-verification context: open invoices, open and past escalations, CRM data, bounded summary, preferences |
| `match_payment` | Compare caller-supplied amount + execution date server-side, return MATCH / NO_MATCH / INSUFFICIENT |
| `propose_allocation` | Move a matched payment UNALLOCATED → UNDER_REVIEW, create ticket, log interaction |
| `request_credit` | Evaluate eligibility and ceilings and issue in one server operation |
| `create_escalation` | Create or append to a ticket, return the structured handoff summary |
| `transfer_to_number` | ElevenLabs system tool |
| `language_detection` | ElevenLabs system tool |

**Rationale**: The original brief listed nine tools. Two (`create_payment_link`, `send_sms`) are out
of scope per FR-048. Three consolidations follow from the spec's own constraints:

- `get_invoice` folded into `get_account_context`. FR-039b already requires open invoices,
  escalations, and CRM data to be loaded live on identification, so a separate invoice fetch would
  duplicate a call that has already happened.
- `check_payment_status` folded into `match_payment`. FR-010 forbids returning stored payment values
  to the conversational layer, so a standalone status tool could return almost nothing useful before
  a match, and nothing the match result does not already carry after one.
- `evaluate_credit_request` and `issue_credit` merged into `request_credit`. Two tools leave a window
  in which the model can call the second without the first; one server operation that evaluates and
  issues atomically removes that window entirely. This is Principle II applied literally.

**Alternatives considered**: Keeping all nine for a closer match to the brief. Rejected — more tools
means worse tool selection by the model, more round trips inside a 5-second budget, and in the credit
case an exploitable gap.

**Consequence**: `create_escalation` takes an optional `existing_ticket_id`, so the address
discrepancy captured after `propose_allocation` (FR-031b) appends to the ticket that already exists
rather than needing a seventh tool.

## D2. Where the discrepancy and handoff text is produced

**Decision**: `create_escalation` composes the structured handoff server-side and returns it as a
string for the agent to speak or pass as `agent_message` on transfer.

**Rationale**: Principle VII requires the handoff to carry specific fields. Composing it in the
backend means the field set is enforced by code and testable, rather than depending on the model
remembering what to include under time pressure.

**Alternatives considered**: Letting the model summarise the call for the human. Rejected — the
summary would then be unverifiable and could omit exactly the facts the human needs.

## D3. Caller-number recognition without disclosure

**Decision**: The conversation initiation webhook resolves `caller_id` to a *candidate* customer id
and a stored language preference, and returns them as dynamic variables. The candidate id is used
only to select the greeting language and to scope subsequent lookups; it carries no verification
weight.

**Rationale**: FR-033a wants the greeting in the caller's language; FR-033b forbids acknowledging
recognition and forbids the number counting as a factor. Passing the id as a secret dynamic variable
keeps it out of the transcript and out of the model's visible context, while `verify_identity`
independently re-derives the customer from the supplied factors.

**Risk**: If `verify_identity` were to trust the candidate id instead of re-deriving it, the phone
number would silently become a verification factor. The verification handler must therefore resolve
the customer from the factors themselves and treat a mismatch with the candidate as a risk signal,
not as an error.

## D4. Transfer failure detection must be in-call, not only post-call

**Decision**: Attempt in-call recovery first — if the transfer tool returns a failure the agent stays
on the line, tells the caller, and the escalation and callback are already persisted. The post-call
webhook is a backstop that reconciles cases where the call ended before recovery.

**Rationale**: FR-020 requires the caller to be told *on the call*. Detecting the failure only from
the post-call payload would satisfy the persistence half of the requirement and fail the human half:
by then the caller has heard silence and hung up.

**Resolved, 2026-09-03, and the answer changes the design rather than settling it.**

The ElevenLabs documentation describes three transfer types and their configuration and says
**nothing about failure**: no failure routing, no recovery path, no statement about whether the agent
regains control. What can be inferred from the mechanisms:

- **Conference** (the default) dials the destination, adds them to a room, and *then* removes the
  agent. The agent plausibly survives a failed dial because it is not removed until the join
  succeeds.
- **Blind** and **SIP REFER** hand the call off at the protocol level. The agent is almost certainly
  gone.

The docs also note that audio is cut the moment a transfer triggers, with no native way to let the
agent finish speaking first.

**Decision**: do not depend on the unverified behaviour. The escalation and the callback are
persisted *before* the transfer is attempted, so the caller is covered either way — if the agent
survives it tells them, and if it does not, a human already has the context and a callback exists.
Conference transfer is used because it is the only type where recovery is even possible.

**Still worth confirming empirically** with one real call to an unroutable number. It determines
whether the demo can *show* the recovery or only describe it, but it no longer determines whether
the caller is protected.

## D5. Timeout budget

**Decision**: Lambda timeout 4s; API Gateway integration timeout 5s; agent-side tool timeout 5s
(FR-023). One retry with backoff for idempotent reads only.

**Rationale**: The Lambda must fail *before* the agent gives up, so the agent receives a structured
error it can speak rather than an opaque timeout. A 1-second margin covers API Gateway overhead.

**Cold start**: Python 3.12 with a trimmed dependency set cold-starts well inside the budget. No
provisioned concurrency — the cost is not justified for a demo, and the first call of a session
warming the path is acceptable. Packages stay small (boto3 is provided by the runtime; only the
HubSpot HTTP client is vendored).

## D6. Idempotency mechanism

**Decision**: DynamoDB conditional writes with `attribute_not_exists(pk)` on a deterministic key.
Allocations and credits key on `conversation_id + entry_id + action`; escalations on
`conversation_id`; post-call processing on `conversation_id`.

**Rationale**: FR-022 and FR-024. A conditional write is atomic and needs no extra table or lock; a
duplicate simply fails the condition and the handler returns the original result rather than an
error, so a retry is indistinguishable from a first call to the caller.

**Alternatives considered**: A dedicated idempotency-key table with TTL. Rejected as an extra table
for no gain at this scale; the natural keys are already unique.

## D7. Region

**Decision**: `eu-central-1` (Frankfurt).

**Rationale**: EU data residency for a Swiss fictional company, full service coverage, and lower
latency to European telephony than us-east-1. `eu-central-2` (Zurich) would be the more thematically
correct choice but has thinner service coverage; the residency argument is satisfied either way.

## D8. Fault simulation

**Decision**: Failures are simulated in tests by patching the adapter layer. No runtime
fault-injection flag ships.

**Rationale**: Clarification session 2 chose test-only coverage. This is why the adapter boundary
matters architecturally: every external dependency sits behind a thin adapter module, so a test can
substitute a failing one without a flag existing in production code.

**Consequence**: the eight required failure scenarios are proven by the test suite, not on camera.
The suite therefore has to be legible in the repo — it *is* the evidence.

## D9. Configuration

**Decision**: Policy values in SSM Parameter Store, read at Lambda cold start with an in-process
cache; secrets in Secrets Manager.

**Values**: `required_factor_count=3`, `credit_max_per_request=100`, `credit_max_rolling=500`,
`credit_window_months=12`, `allocation_authority_max=0` (all allocations reviewed),
`tool_timeout_seconds=5`, `read_retry_count=1`, `transcript_retention_days=90`,
`summary_max_chars=2000`, `verification_max_attempts=3`.

**Rationale**: Principle VIII. Caching at cold start rather than per invocation keeps the parameter
read off the hot path; a policy change takes effect within one Lambda lifecycle, which is acceptable.

## D10. Credit capped by the entry it is applied to

**Decision**: `request_credit` enforces `credit_amount <= entry_amount - credits_already_on_entry`
and returns `DENIED_EXCEEDS_ENTRY` when it does not hold (FR-013d, confirmed 2026-09-02).

**Rationale**: The two policy ceilings are about how much goodwill a customer may accumulate; this is
about not crediting more than was charged. Keeping them separate means the check survives any future
change to the CHF 100 / CHF 500 values, and the denial reason tells the human why.

**Placement**: in `src/domain/credit.py` alongside the ceiling checks, evaluated before them so that
the most specific reason is the one reported.

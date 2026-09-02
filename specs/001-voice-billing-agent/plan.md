# Implementation Plan: Multilingual Voice Billing Agent

**Branch**: `001-voice-billing-agent` | **Date**: 2026-09-02 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-voice-billing-agent/spec.md`

## Summary

An inbound voice agent for a fictional Swiss B2B company handles invoice, payment, credit, and
billing-dispute calls in German, French, Italian, and English. ElevenLabs owns the dialogue, language
detection, tool selection, and transfer; every decision that matters financially is made by a Python
Lambda behind API Gateway, against DynamoDB and S3, with HubSpot holding only non-sensitive CRM
records. The agent asks, explains, and recommends; it never decides.

The golden path — a disputed CHF 4,200 overdue invoice, matched server-side against an unallocated
payment, proposed for human allocation, ticketed, and handed off — exercises verification, the
refusal to invent state, out-of-authority escalation, CRM logging, and post-call persistence in one
continuous journey.

## Technical Context

**Language/Version**: Python 3.12 (Lambda), HCL (Terraform ≥1.10 — S3-native state locking)

**Primary Dependencies**: AWS Lambda, API Gateway (HTTP API), DynamoDB, S3, KMS, Secrets Manager,
SSM Parameter Store, CloudWatch. ElevenLabs Agents (server tools, system tools, conversation
initiation and post-call webhooks). HubSpot REST API v3. Twilio (inbound number only). boto3 from the
runtime; `httpx` vendored for HubSpot.

**Storage**: DynamoDB — `customer_identity` (KMS CMK), `ledger`, `conversations`,
`customer_summaries`. S3 for transcripts with a 90-day lifecycle. CloudWatch Logs for audit events
with 10-year retention. See [data-model.md](./data-model.md).

**Testing**: pytest. Pure domain logic in `src/domain/` is tested without AWS; adapters are patched to
simulate every required failure. One integration test drives the golden path against deployed
endpoints.

**Target Platform**: AWS serverless, `eu-central-1`.

**Project Type**: Backend service plus versioned agent configuration and Terraform infrastructure.

**Performance Goals**: Tool responses inside the agent's 5-second budget; Lambda timeout 4s so the
backend fails before the agent gives up and can return a speakable structured error.

**Constraints**: No financial disclosure before three verified factors. No LLM-authored mutation.
`UNKNOWN`/`SERVICE_UNAVAILABLE` never rendered as an answer. All mutations idempotent. Synthetic data
only. Roughly 20 hours of build, demoed in 3–5 minutes.

**Scale/Scope**: Demo scale — a handful of seeded customers, single concurrent call. Designed so the
concurrency assumptions (conditional writes, idempotency keys) hold if that changed.

## Constitution Check

*GATE: passed before Phase 0, re-checked after Phase 1 design.*

| Principle | How the design satisfies it | Status |
|---|---|---|
| I. Verification before disclosure | `get_account_context`, `match_payment`, `propose_allocation`, `request_credit` all refuse unless the conversation is VERIFIED server-side. Verification state lives in the backend, keyed by conversation, not in the prompt. | PASS |
| II. Server-side authority | Every rule runs in `src/domain/`, called only from Lambda. `request_credit` merges evaluation and issuance so the model cannot issue without evaluating. Caller-id recognition is explicitly stripped of verification weight. | PASS |
| III. Never invent state | Tools return enumerated statuses with a `message_hint` that never asserts a financial fact. `SERVICE_UNAVAILABLE` is a distinct status from `NO_MATCH`. `MATCH` is impossible on an unsettled payment record. | PASS |
| IV. Data minimisation | `customer_identity` has its own CMK and a restricted IAM policy; only `verify_identity` and `conversation_init` can read it. `match_payment` returns booleans, never the stored address or amount. HubSpot receives name, company, owner, tickets, interactions — nothing else. | PASS |
| V. Escalation first-class | `create_escalation` composes the handoff server-side so its fields are enforced by code. In-call recovery on transfer failure, with the post-call webhook as backstop. | PASS with risk — see D4 |
| VI. Idempotency & bounded retries | Conditional writes on natural keys for allocations, credits, escalations, and post-call. One retry on idempotent reads only; mutations never retried. | PASS |
| VII. Auditability | Structured `AUDIT` log events carry all eleven FR-041 fields; 10-year log retention. `conversations` holds the metrics from which all nine FR-043 rates derive. | PASS |
| VIII. Configurable policy | Eleven policy values in SSM, cached at cold start. No threshold literal in any prompt or handler. | PASS |
| IX. Scope discipline | Build order below follows the constitution's priority order. Payment links, SMS, and auto-reconciliation are out of scope per FR-048/FR-049. | PASS |
| X. Security baseline | Per-Lambda least-privilege IAM, KMS at rest, Secrets Manager, API-key auth on tools, HMAC on the post-call webhook, server-side validation on every input, no identity fields in logs. GitHub Actions uses OIDC, no long-lived keys. | PASS |

**One risk, not a violation**: Principle V requires the caller to be told on the call when a transfer
fails. Whether that is achievable depends on whether the ElevenLabs transfer tool returns control to
the agent on a failed dial (research D4). If it does not, the honest position is that the fallback
persists the escalation and creates a callback but cannot speak to the caller — and the demo must say
so rather than imply otherwise.

## Project Structure

### Documentation (this feature)

```text
specs/001-voice-billing-agent/
├── plan.md              # This file
├── spec.md
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/           # Phase 1
│   ├── tools.md
│   └── webhooks.md
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks output — not created here
```

### Source Code (repository root)

```text
src/
├── domain/                  # Pure business rules. No AWS, no I/O, no LLM. Fully unit-tested
│   ├── verification.py      # factor counting, non-document rule, lockout
│   ├── payment_match.py     # exact amount + exact execution date
│   ├── allocation.py        # authority check, state transition legality
│   ├── credit.py            # eligibility, entry-amount cap, both ceilings, rolling window
│   ├── risk.py              # signal detection and the risk override
│   ├── handoff.py           # structured handoff composition
│   └── policy.py            # typed config object loaded from SSM
├── adapters/                # The only code that talks to anything external
│   ├── dynamo.py  ├── s3.py  ├── hubspot.py
│   ├── secrets.py ├── ssm.py └── errors.py   # external failure → error category
├── handlers/                # One Lambda entry point each, thin: parse, authorise, call domain, log
│   ├── verify_identity.py       ├── get_account_context.py
│   ├── match_payment.py         ├── propose_allocation.py
│   ├── request_credit.py        ├── create_escalation.py
│   ├── conversation_init.py     └── post_call.py
└── common/                  # auth, structured logging, idempotency, request validation

agent/                       # ElevenLabs agent config, versioned in git
├── prompt/{de,fr,it,en}.md  # system prompt per language
├── tools.json               # webhook tool definitions matching contracts/tools.md
└── agent.json               # voice, language detection, transfer configuration

infra/terraform/             # api_gateway, lambda, dynamodb, s3, kms, iam, secrets, ssm, logs
scripts/seed/                # synthetic fixtures
tests/
├── unit/                    # domain rules and the eight failure scenarios via patched adapters
├── contract/                # handler request/response shapes against contracts/
└── integration/             # golden path against deployed endpoints
.github/workflows/           # pr.yml, main.yml
```

**Structure Decision**: The `domain / adapters / handlers` split is the load-bearing choice. It is
what makes Principle II checkable rather than aspirational — the financial rules are pure functions
with no way to reach the network — and it is what makes the eight required failure scenarios testable
without a runtime fault-injection flag, since a test substitutes a failing adapter (research D8).
Handlers stay thin enough that reviewing them for "does this enforce verification first" is quick.

## Build Order

Following Principle IX. Each step leaves the system demonstrable.

1. **Terraform skeleton + one tool end to end.** API Gateway, one Lambda, DynamoDB, secrets, OIDC
   deploy. Proves the pipeline before any business logic exists.
2. **Verification.** `verify_identity` plus the identity table, lockout, and risk signal. The gate
   before anything else can be built safely.
3. **Golden path financial core.** `get_account_context`, `match_payment`, `propose_allocation`,
   ledger table, seed fixtures.
4. **Escalation and handoff.** `create_escalation`, HubSpot tickets and interaction logs, transfer
   wiring, in-call failure recovery.
5. **Credit rules.** `request_credit`, rolling window, risk override, the abuse case.
6. **Agent configuration and multilingual.** Prompts in four languages, language detection, greeting
   language from the initiation webhook, adaptive pace.
7. **Post-call persistence.** Transcripts to S3, metrics to `conversations`, summary regeneration,
   transfer reconciliation.
8. **Audit, metrics, and the failure test suite.** The eight scenarios, the nine derived rates.
9. **Hardening.** Retention lifecycles, IAM tightening, log scrubbing verification.

## Complexity Tracking

No constitutional violations to justify. Three deliberate simplifications, recorded so they are
choices rather than omissions:

| Simplification | Why | What was rejected |
|---|---|---|
| Audit events in CloudWatch Logs, not DynamoDB | Append-only, never read by key during a call; log retention and Insights cover review | An audit table — extra IAM surface and Terraform for no in-call read |
| Risk signals inside `conversations`, not their own table | Every signal is conversation-scoped and read only as an aggregate | A fifth table for a one-to-one relationship |
| No runtime fault-injection flag | Clarification chose test-only failure coverage | A per-dependency toggle — would have made the failures demoable live |

## Deviations from the stack input

Three items in the requested stack contradict the clarified spec. The spec governs:

| Requested | Spec | Resolution |
|---|---|---|
| SMS via Twilio on transfer failure | FR-048 puts outbound messaging out of scope; FR-020 makes the fallback self-contained | No SMS. Fallback is persist escalation + callback + tell the caller on the call |
| Reconciliation Lambda, reconciliation-confidence tests | FR-049 drops auto-reconciliation | The agent asks the caller moved-or-typo, records it on the existing ticket, a human corrects |
| Failure scenarios via runtime config toggles | Clarification chose test-only | Adapters patched in tests; no toggle ships |

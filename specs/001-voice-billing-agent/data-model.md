# Phase 1 Data Model

**Date**: 2026-09-02 | **Spec**: [spec.md](./spec.md) | **Research**: [research.md](./research.md)

Four DynamoDB tables and one S3 prefix. Separation is by sensitivity and retention class, not by
convenience: identity is the only store with a customer-managed KMS key and a restricted IAM policy,
and transcripts are the only artefact that expires in 90 days.

## `customer_identity`

Sensitive. KMS CMK. Read by `verify_identity` and `conversation_init` only; no other Lambda has
`dynamodb:GetItem` on it.

| Attribute | Type | Notes |
|---|---|---|
| `customer_id` (PK) | S | `cust_<ulid>` |
| `company_name` | S | |
| `first_name`, `last_name` | S | |
| `date_of_birth` | S | ISO date. Verification factor. Never leaves the backend |
| `postal_address` | M | street, postcode, city, country |
| `phone`, `email` | S | Verification factors |
| `account_opening_year` | N | Verification factor |
| `account_status` | S | `ACTIVE` \| `SUSPENDED` \| `COLLECTIONS` \| `CLOSED` |
| `hubspot_contact_id`, `hubspot_company_id` | S | Link out to the CRM |
| `preferred_language` | S | `de` \| `fr` \| `it` \| `en` |
| `communication_style` | M | pace, complexity hints (FR-036) |
| `failed_verification_attempts` | N | Reset on success |
| `locked_until` | S | ISO timestamp, set when attempts exceed the configured limit |

**GSI `phone-index`**: `phone` → `customer_id`, `preferred_language`. Used by `conversation_init`
only, to pick a greeting language. Returns nothing else.

**Validation**: `account_status` must be `ACTIVE` for `request_credit` to grant (FR-013).

## `ledger`

One table for every financial entry (FR-017: balances are derived, never stored).

| Attribute | Type | Notes |
|---|---|---|
| `customer_id` (PK) | S | |
| `entry_id` (SK) | S | `inv_*`, `pay_*`, `cn_*`, `adj_*` |
| `type` | S | `INVOICE` \| `PAYMENT` \| `CREDIT_NOTE` \| `ADJUSTMENT` |
| `entry_date` | S | For payments this is the **execution date** — what the payer sees in their bank (FR-010b) |
| `due_date` | S | Invoices only |
| `amount` | N | Signed. Invoices positive, payments and credits negative |
| `currency` | S | `CHF` |
| `status` | S | See transitions below |
| `reference` | S | Nullable — the golden-path payment has none |
| `allocated_to` | L | Entry ids this entry settles |
| `payer_name`, `payer_address` | M | Payment only. `payer_address` is the golden path's mismatch |
| `reason` | S | Credits and adjustments |
| `conversation_id` | S | Originating conversation, when created by a call |
| `decision_source` | S | `AGENT_AUTONOMOUS` \| `AGENT_PROPOSED` \| `HUMAN` \| `SEED` |
| `approval_status` | S | `NOT_REQUIRED` \| `PENDING` \| `APPROVED` \| `REJECTED` |

**GSI `status-index`**: `customer_id` + `status`. Serves open-invoice and unallocated-payment lookups
without a table scan.

**State transitions**

- Invoice: `OPEN` → `OVERDUE` (due date passed) → `PAID` (fully allocated).
- Payment: `UNALLOCATED` → `UNDER_REVIEW` (propose_allocation, FR-012) → `ALLOCATED` (human approves)
  or back to `UNALLOCATED` (human rejects).
- Credit note: `APPROVED` on autonomous grant, or `PENDING_APPROVAL` when escalated.

`propose_allocation` writes `UNALLOCATED → UNDER_REVIEW` conditionally on the current status still
being `UNALLOCATED`, so a concurrent second call cannot double-propose.

## `conversations`

Metadata and per-interaction metrics (FR-042). 10-year retention. No transcript text.

| Attribute | Type | Notes |
|---|---|---|
| `conversation_id` (PK) | S | Also the idempotency key for post-call processing |
| `customer_id` | S | Null when verification never succeeded |
| `agent_id`, `agent_version` | S | |
| `started_at`, `ended_at`, `duration_seconds` | S / N | |
| `language` | S | Final language of the call |
| `verification` | M | duration, result, attempt count |
| `tools_invoked` | M | name → {count, total_latency_ms, failures, retries} |
| `financial_actions` | L | attempted and completed, with entry ids |
| `credits_issued` | N | |
| `escalation` | M | required, reason, ticket id |
| `transfer` | M | attempted, result |
| `outcome` | S | `RESOLVED_AUTONOMOUS` \| `ESCALATED` \| `TRANSFERRED` \| `ABANDONED` \| `FAILED` |
| `error_category` | S | Nullable |
| `transcript_s3_key` | S | Pointer, not content |

The nine derived rates in FR-043 are computed from these attributes; none is stored precomputed.

## `customer_summaries`

One item per customer (FR-039). Regenerated after every call, capped at ~2,000 characters.

| Attribute | Type | Notes |
|---|---|---|
| `customer_id` (PK) | S | |
| `summary_text` | S | ≤ `summary_max_chars`. Narrative and preferences only |
| `source_conversation_ids` | L | Provenance (FR-039) |
| `updated_at` | S | |
| `version` | N | Incremented per regeneration; guards concurrent writes |

**Invariant**: contains no verification answers, no date of birth, no payment credentials (FR-029),
and is never the source for a financial statement (FR-039c). Open invoices and escalations are read
live from `ledger` and HubSpot on every call, not from here.

## S3 transcripts

`customers/{customer_id}/conversations/{YYYY}/{MM}/{conversation_id}.json`, SSE-KMS, lifecycle
expiration at `transcript_retention_days` (90). The only artefact with a short life.

## Audit events

Written to CloudWatch Logs as structured JSON with a dedicated `event_type: AUDIT` and a 10-year log
group retention, carrying every field in FR-041: action, previous state, new state, authorizing rule,
identifiers, timestamp, agent version, risk result, human-approval flag.

**Rationale for not using a DynamoDB table**: audit events are append-only and never queried by key
during a call. A log group with retention and CloudWatch Logs Insights covers the review use case at
a fraction of the setup. If audit ever needs to be read back in-call, it moves to a table.

## Risk signals

Stored in `conversations` under a `risk_signals` list rather than in their own table: every signal is
raised during a conversation and is read back only as an aggregate over the customer's recent
conversations. The GSI `customer-index` on `conversations` supports that read.

**Rationale**: FR-016 requires type, timestamp, evidence, and conversation on every signal — all of
which are already conversation-scoped. A fifth table would add IAM surface and Terraform for a
one-to-one relationship.

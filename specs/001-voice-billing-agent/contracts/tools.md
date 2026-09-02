# Tool Contracts

Six server tools, exposed as ElevenLabs webhook tools through API Gateway. Every request carries
`X-Api-Key` (Secrets Manager) and `conversation_id`. Every response is a structured status — never a
raw sensitive field (Principle IV, FR-026).

Common error envelope, returned instead of the success body on any failure:

```json
{ "status": "SERVICE_UNAVAILABLE",
  "error_category": "TIMEOUT|DEPENDENCY_DOWN|VALIDATION|NOT_AUTHORIZED|INTERNAL",
  "retryable": true,
  "message_hint": "A short, caller-safe phrase the agent may paraphrase" }
```

`message_hint` never asserts a financial fact. The agent must treat `SERVICE_UNAVAILABLE` as "cannot
tell you right now", never as an answer (FR-011).

---

## `verify_identity`

```json
// request
{ "conversation_id": "conv_...",
  "factors": [ { "field": "email|phone|date_of_birth|account_opening_year|customer_id",
                 "value": "as spoken by the caller" } ] }

// response
{ "status": "VERIFIED|PARTIALLY_VERIFIED|FAILED|LOCKED",
  "factors_confirmed": 2,
  "factors_required": 3,
  "next_factor_hint": "email|phone|date_of_birth|account_opening_year|customer_id",
  "non_document_factor_satisfied": false,
  "locked_until": null }
```

- Resolves the customer from the factors themselves, never from the caller-id candidate (research D3).
- `next_factor_hint` names a *field to ask for*. It never carries a value (FR-004).
- Never reveals which specific factor was wrong (FR-004).
- `VERIFIED` requires three confirmed factors including at least one non-document factor (FR-003a).
- Increments `failed_verification_attempts`; sets `locked_until` and returns `LOCKED` past the limit
  (FR-006), writing a risk signal.

## `get_account_context`

```json
// request
{ "conversation_id": "conv_..." }

// response
{ "status": "OK",
  "customer": { "company_name": "...", "preferred_language": "de", "communication_style": {...} },
  "open_invoices": [ { "entry_id": "inv_...", "amount": 4200.00, "currency": "CHF",
                       "due_date": "2026-07-15", "status": "OVERDUE" } ],
  "open_escalations": [ { "ticket_id": "...", "reason": "...", "opened_at": "..." } ],
  "past_escalations": [ { "ticket_id": "...", "reason": "...", "closed_at": "...", "outcome": "..." } ],
  "crm": { "owner": "...", "last_interaction_at": "..." },
  "summary_text": "≤2000 chars, narrative and preferences only" }
```

- **Refuses unless the conversation is VERIFIED** (FR-001). Returns `NOT_AUTHORIZED` otherwise.
- Fetches invoices, escalations and CRM data live; the summary is supplementary (FR-039b).

## `match_payment`

```json
// request
{ "conversation_id": "conv_...", "invoice_entry_id": "inv_...",
  "claimed_amount": 4200.00, "claimed_execution_date": "2026-07-02" }

// response
{ "status": "MATCH|NO_MATCH|INSUFFICIENT",
  "payment_entry_id": "pay_...",        // present only on MATCH
  "covers_invoice": true,                // present only on MATCH
  "requires_human_allocation": true,
  "address_discrepancy": true,           // present only on MATCH
  "missing_fields": ["claimed_execution_date"] }  // INSUFFICIENT only
```

- Exact amount and exact execution date, no tolerance (FR-010a).
- `NO_MATCH` when a supplied value differs; `INSUFFICIENT` when a field is absent or more than one
  candidate payment fits.
- Never returns the stored amount, date, reference, or payer address (FR-010). `address_discrepancy`
  is a boolean, not the address.
- Never returns `MATCH` for a payment that is not in a settled state (FR-010d).

## `propose_allocation`

```json
// request
{ "conversation_id": "conv_...", "payment_entry_id": "pay_...", "invoice_entry_id": "inv_..." }

// response
{ "status": "UNDER_REVIEW|ALREADY_UNDER_REVIEW|NOT_AUTHORIZED|CONFLICT",
  "ticket_id": "...", "previous_status": "UNALLOCATED", "new_status": "UNDER_REVIEW",
  "resolution_target_hours": 24 }
```

- Conditional write on `status = UNALLOCATED`; a duplicate returns `ALREADY_UNDER_REVIEW` with the
  original ticket, not an error (FR-022).
- Writes the audit event, creates the HubSpot ticket, logs the interaction (FR-012, FR-041, FR-044).
- Never transitions straight to `ALLOCATED` — that is the human's action.

## `request_credit`

```json
// request
{ "conversation_id": "conv_...", "entry_id": "inv_...",
  "amount": 40.00, "reason": "caller's stated reason" }

// response
{ "status": "GRANTED|DENIED_LIMIT|DENIED_RISK|DENIED_INELIGIBLE|DENIED_EXCEEDS_ENTRY|ESCALATE",
  "credit_entry_id": "cn_...",       // GRANTED only
  "rule_applied": "credit_max_per_request|credit_max_rolling|entry_amount_cap|risk_override|account_status|open_dispute",
  "rolling_total_after": 500.00 }
```

- Evaluates and issues in one operation, so evaluation cannot be skipped (research D1).
- Both ceilings inclusive: request ≤ 100, and 12-month total including this request ≤ 500 (FR-013b).
- `DENIED_RISK` when a risk signal fires; risk overrides passing eligibility (FR-015).
- `DENIED_INELIGIBLE` when the account is not active, the entry does not exist, or the entry has an
  open dispute (FR-013).
- `DENIED_EXCEEDS_ENTRY` when the credit would exceed the entry's amount less credits already applied
  to it (FR-013d). This is checked independently of the policy ceilings, so a CHF 100 request against
  a CHF 20 charge is refused even though CHF 100 is within the per-request limit.
- Idempotent on `conversation_id + entry_id + amount`.

## `create_escalation`

```json
// request
{ "conversation_id": "conv_...", "reason": "PAYMENT_UNVERIFIABLE|CREDIT_ABOVE_AUTHORITY|...",
  "existing_ticket_id": null,
  "notes": "caller says the company moved in March",
  "discrepancy": { "field": "payer_address", "caller_explanation": "MOVED|TYPO|UNKNOWN" } }

// response
{ "status": "CREATED|APPENDED|CRM_UNAVAILABLE_PERSISTED",
  "ticket_id": "...",
  "handoff_summary": "Composed server-side. Verified state, intent, facts gathered, actions attempted and their outcomes, escalation reason.",
  "callback_created": false }
```

- `existing_ticket_id` appends rather than creating a second ticket (FR-031b).
- `handoff_summary` is composed in the backend so its field set is enforced and testable (FR-019,
  research D2). The agent passes it as `agent_message` on transfer.
- If HubSpot is unavailable, the escalation is persisted locally and `CRM_UNAVAILABLE_PERSISTED` is
  returned — the escalation is never lost to a CRM outage (FR-025).

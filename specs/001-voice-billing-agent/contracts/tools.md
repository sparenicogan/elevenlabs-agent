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
// request — one flat field per detail, omitting any the caller did not give
{ "conversation_id": "conv_...",
  "email": "...",
  "phone": "...",
  "date_of_birth": "..." }

// response
{ "status": "VERIFIED|PARTIALLY_VERIFIED|FAILED|LOCKED",
  "factors_confirmed": 2,
  "factors_required": 3,
  "personal_factor_satisfied": false }
```

- Flat fields rather than a list of `{field, value}` objects. The nested shape was correct and
  the model could not reliably produce it: on a real call it sent the array as a JSON string with
  one entry carrying `field` twice, after all three details had already matched individually.
- Resolves the customer from the details themselves, never from the caller-id candidate
  (research D3).
- Never reveals which specific detail was wrong, and never says what to ask for next (FR-004).
- `VERIFIED` requires three confirmed factors including at least one personal factor (FR-003a).
- A detail that cannot be parsed at all is neither confirmed nor a mismatch: unreadable is not
  wrong, and it must not discard the details that matched.

## `get_account_context`

```json
// request
{ "conversation_id": "conv_..." }

// response
{ "status": "OK",
  "customer": { "company_name": "...", "preferred_language": "de", "communication_style": {...} },
  "recent_invoices": [ ... ],   // settled, newest first — what a credit attaches to
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
  "claimed_amount": 4200.00, "claimed_transfer_date": "2026-07-02" }

// response
{ "status": "MATCH|NO_MATCH|INSUFFICIENT",
  "payment_entry_id": "pay_...",        // present only on MATCH
  "covers_invoice": true,                // present only on MATCH
  "requires_human_allocation": true,
  "address_discrepancy": true,           // present only on MATCH
  "payer_address": "Alte Landstrasse 88, 8702 Zollikon",  // MATCH, and only when it differs
  "missing_fields": ["claimed_execution_date"] }  // INSUFFICIENT only
```

- Exact amount, no tolerance. Date within `payment_date_tolerance_days` (default 3) *before* the
  recorded date, never after (FR-010a, FR-010b).
- `NO_MATCH` when a supplied value differs; `INSUFFICIENT` when a field is absent or more than one
  candidate payment fits.
- Never returns the stored amount, date or reference (FR-010).
- Returns `payer_address` only on a MATCH and only when it differs from the address on file. By
  then the caller has proved who they are and proved the payment is theirs by stating its amount
  and exact date, so it cannot be fished for — and asking whether it is a typo without saying
  what it is asks someone to confirm what they cannot see. The address on file is never returned.
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
{ "conversation_id": "conv_...", "reason": "PAYMENT_UNVERIFIABLE|CREDIT_ABOVE_AUTHORITY|IDENTITY_NOT_ESTABLISHED|...",
  "existing_ticket_id": null,
  "notes": "caller says the company moved in March",
  "caller_stated_problem": "in the caller's own words, verbatim",
  "caller_self_description": "what they said about who they are — unverified",
  "discrepancy": { "field": "payer_address", "caller_explanation": "MOVED|TYPO|UNKNOWN" } }

// response
{ "say_before_transferring": "A colleague has the details and will call you back if we get cut off.",
  "status": "CREATED|APPENDED|CRM_UNAVAILABLE_PERSISTED",
  "ticket_id": "...",
  "handoff_summary": "Composed server-side. Verified state, intent, facts gathered, actions attempted and their outcomes, escalation reason.",
  "callback_created": false }
```

- `say_before_transferring` is returned first and named for the act, because a transfer can drop
  the call and the promise is worth nothing said afterwards. It is a nudge, not a guarantee: the
  callback is recorded server-side either way (FR-019c).
- `existing_ticket_id` appends rather than creating a second ticket (FR-031b).
- `handoff_summary` is composed in the backend so its field set is enforced and testable (FR-019,
  research D2). The agent passes it as `agent_message` on transfer.
- If HubSpot is unavailable, the escalation is persisted locally and `CRM_UNAVAILABLE_PERSISTED` is
  returned — the escalation is never lost to a CRM outage (FR-025).
- **Works without a verified conversation.** `IDENTITY_NOT_ESTABLISHED` is the one reason accepted
  from an unverified call, so a caller the system cannot identify still reaches a human with their
  problem already written down (FR-019b).
- On an unverified escalation the ticket is created with **no contact or company association**
  (FR-019d), the handoff carries no account fact of any kind (FR-019c), and
  `caller_self_description` is labelled unverified wherever it is shown.
- `caller_stated_problem` and `caller_self_description` are untrusted caller text. They are recorded
  and displayed, never interpreted as instructions (FR-019e).

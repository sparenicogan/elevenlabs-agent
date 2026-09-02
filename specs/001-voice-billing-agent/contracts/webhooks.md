# Webhook Contracts

## Conversation initiation (inbound)

`POST /webhooks/conversation-initiation` — called by ElevenLabs when a call arrives.

```json
// request (from ElevenLabs)
{ "caller_id": "+41...", "agent_id": "...", "called_number": "+15513216408", "call_sid": "..." }

// response
{ "dynamic_variables": {
    "secret__candidate_customer_id": "cust_...",   // null when the number is unknown
    "greeting_language": "de" },
  "conversation_config_override": { "agent": { "language": "de" } } }
```

- The candidate id is passed as a `secret__` variable so it stays out of the transcript and the
  model's visible context.
- **It carries no verification weight.** `verify_identity` re-derives the customer from the supplied
  factors; a mismatch between the two is recorded as a risk signal, not treated as an error
  (FR-033b, research D3).
- `greeting_language` falls back to `de` when the number is unrecognised (FR-033a).
- The agent must not greet by name or acknowledge recognition.

## Post-call

`POST /webhooks/post-call` — called by ElevenLabs after the call ends.

- **Authentication**: `ElevenLabs-Signature` header, HMAC-SHA256 over `timestamp.body` with the
  shared secret. Reject if the timestamp is outside a 30-minute window or the digest does not match.
- **Idempotency**: `conversation_id` is the key. A conditional write on `conversations` means a
  duplicate delivery creates nothing twice and returns 200 (FR-024).
- **Provider retries**: disabled; the handler dedupes regardless.

Processing order, each step independently failure-tolerant:

1. Persist the raw payload and transcript to S3.
2. Write structured metadata and metrics to `conversations`.
3. Regenerate the customer summary from the prior summary plus this call, capped at
   `summary_max_chars`, and write it with a version check.
4. Reconcile the transfer outcome: if the transfer failed and no callback exists, persist the
   callback requirement and mark the escalation for human follow-up (FR-020 backstop, research D4).

A failure in any step is logged with its error category and does not roll back the preceding steps —
partial progress is recorded rather than discarded (FR-025).

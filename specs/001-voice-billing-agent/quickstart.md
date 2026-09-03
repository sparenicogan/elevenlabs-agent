# Quickstart & Validation Guide

**Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Contracts**: [contracts/](./contracts/)

How to stand the system up and prove it does what the spec claims. Implementation detail belongs in
`tasks.md`; this is the run-and-verify guide.

## Prerequisites

- A dedicated AWS account for this demo, with permission to create Lambda, API Gateway, DynamoDB,
  S3, KMS, IAM, SSM, Secrets Manager in `eu-central-1`. The account id goes in an untracked
  `infra/terraform/terraform.tfvars`, and the provider's `allowed_account_ids` guard makes an apply
  against any other account fail
- Terraform ≥ 1.10 (S3-native state locking), Python 3.12, `uv` or `pip`
- ElevenLabs account with Agents access
- HubSpot developer account and a private-app token
- The inbound number `the provisioned inbound number` routed to the ElevenLabs agent
- A second phone number to act as the billing-specialist transfer destination

Secrets go to Secrets Manager, never to the repo:

```
/voice-agent/elevenlabs/webhook-secret     # HMAC for the post-call webhook
/voice-agent/tools/api-key                 # X-Api-Key the agent sends to tool endpoints
/voice-agent/hubspot/private-app-token
```

## Deploy

One-time bootstrap, because Terraform cannot create the bucket it uses as a backend:

```bash
aws sso login
aws s3api create-bucket --bucket <state-bucket> \
  --region eu-central-1 --create-bucket-configuration LocationConstraint=eu-central-1
aws s3api put-bucket-versioning --bucket <state-bucket> \
  --versioning-configuration Status=Enabled
```

Then, every time:

```bash
cd infra/terraform && terraform init && terraform apply
python scripts/seed/seed.py --env dev        # synthetic fixtures only
```

The first apply runs under your own credentials and creates the OIDC deploy role. After it
succeeds, CI takes over and you never apply locally again.

Then point the ElevenLabs agent at the deployed URLs: tool endpoints per
[contracts/tools.md](./contracts/tools.md), the two webhooks per
[contracts/webhooks.md](./contracts/webhooks.md), and the transfer destination.

## Seeded fixtures

| Customer | Purpose |
|---|---|
| **Meier Bau AG** | Golden path. Overdue CHF 4,200 invoice; unallocated CHF 4,200 payment with no reference and a mismatched payer address |
| **Rossi Logistica Sagl** | Clean credit case. No credit history, active account, Italian preference |
| **Dubois Conseil Sàrl** | Abuse case. CHF 460 of credits inside the rolling 12 months, French preference |
| **Weber Handels GmbH** | Unknown-number case. No phone on file, so the greeting falls back to German |

## Validating the user stories

Each maps to a story in the spec. Run them in this order — later ones assume the earlier ones pass.

### US1 — golden path (the demo)

Call the number as Meier Bau AG and say the CHF 4,200 invoice was already paid.

Expect, in order: no financial detail before verification; three factors requested including one not
printed on an invoice; invoice reported overdue with no linked payment; a request for the exact amount
and exact transfer date with an offer to check banking records; on exact answers a MATCH; an
explanation that a person must validate the allocation; the address question (moved or typo?); a
resolution promise of 24 hours.

Verify afterwards:

```bash
aws dynamodb get-item --table-name ledger \
  --key '{"customer_id":{"S":"cust_meier"},"entry_id":{"S":"pay_..."}}'   # status UNDER_REVIEW
aws s3 ls s3://<bucket>/customers/cust_meier/conversations/2026/09/       # transcript present
aws logs filter-log-events --log-group-name /voice-agent/audit \
  --filter-pattern '{ $.event_type = "AUDIT" }'                          # previous+new state, rule
```

And in HubSpot: one ticket carrying the structured context, associated to the contact and company,
with the address answer appended to that same ticket — not a second one.

### US2 — verification gate

Before verifying, ask directly for the balance, assert your identity, claim urgency, and claim to be a
returning caller. Nothing financial may be disclosed on any attempt. Then fail three factors in a row
and confirm the response becomes `LOCKED`, a risk signal is written, and the agent escalates rather
than continuing to ask.

Confirm too that three factors all printed on an invoice do not verify — the non-document rule.

### US3 / US4 — credits

As Rossi, request CHF 40 against a named charge → granted on the call, ledger entry written, audit
event, CRM log. Then check the boundaries: CHF 100 with CHF 400 of history is granted at exactly
CHF 500; CHF 100 with CHF 401 of history is not; CHF 101 is never granted.

As Dubois (CHF 460 of history), request CHF 80 → not granted, risk signal written, escalation created,
and the wording carries no accusation.

### US5 — failure paths

These are proven by the test suite, not on a live call (research D8):

```bash
pytest tests/unit/test_failure_scenarios.py -v
```

Eight scenarios: identity store down, invoice store down, payment lookup down, CRM down, transfer
failure, duplicate post-call webhook, partial failure after a mutation, and a slow dependency hitting
the 4-second Lambda timeout. Each asserts a caller-safe outcome with a next step and no fabricated
financial statement.

Transfer failure is the one that is also demonstrable live: repoint the destination number at an
unroutable value and call in.

### US6 — multilingual

Run the golden path in German, French, Italian, and English. No turn may fall out of the caller's
language, including tool-driven statements and the escalation message. Amounts and dates follow locale
convention while staying numerically exact. Call as Rossi from the number on file and confirm the
greeting is Italian without the agent using a name or acknowledging recognition. Call as Weber and
confirm German.

### US7 — memory and metrics

Call twice as the same customer. The second call must load the summary written by the first *and*
fetch open invoices, escalations, and CRM data live. Confirm the summary stays within ~2,000
characters, contains no verification answers or date of birth, and that where it disagrees with a live
source the agent speaks the live value.

Replay the post-call webhook with the same `conversation_id` and confirm nothing is created twice.

### US8 — address discrepancy

Covered inside US1. Confirm the discrepancy is raised only *after* the payment match, never before,
and that no address field was written by the agent.

## Test suite

```bash
pytest tests/unit          # domain rules, no AWS
pytest tests/contract      # handler shapes against contracts/
pytest tests/integration   # golden path against deployed endpoints
```

## Before recording the Loom

- [ ] Golden path runs clean end to end at least twice
- [ ] Verification cannot be talked past
- [ ] `pytest` green, and the failure tests are legible on screen — with test-only coverage they are
      the evidence for a third of the spec
- [ ] No identity field in any CloudWatch log line
- [ ] HubSpot holds no date of birth, verification answer, or payment detail
- [ ] Transfer destination set to your own phone, and you can answer it
- [ ] The 3–5 minute cut shows the golden path plus one failure path

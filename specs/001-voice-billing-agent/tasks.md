---

description: "Task list for the multilingual voice billing agent"
---

# Tasks: Multilingual Voice Billing Agent

**Input**: Design documents from `/specs/001-voice-billing-agent/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/)

**Tests**: Included. SC-005 makes the test suite the *only* evidence for the eight failure scenarios (clarification chose test-only coverage), so tests are a deliverable here, not an optional extra.

**Organization**: Grouped by user story, in the spec's priority order, which follows Constitution IX.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on incomplete work)
- **[Story]**: US1–US8, matching spec.md

## Path Conventions

Per plan.md: `src/domain/` (pure rules), `src/adapters/` (all external I/O), `src/handlers/` (thin Lambda entry points), `src/common/`, `agent/`, `infra/terraform/`, `scripts/seed/`, `tests/`.

---

## Phase 1: Setup

**Purpose**: Repository, tooling, and the deploy pipeline — before any business logic exists.

- [x] T001 Create the directory structure from plan.md in the repository root: `src/{domain,adapters,handlers,common}/`, `agent/prompt/`, `infra/terraform/`, `scripts/seed/`, `tests/{unit,contract,integration}/`, each with `__init__.py` where it is a Python package
- [x] T002 Initialise the Python project in `pyproject.toml`: Python 3.12, `httpx` as the only runtime dependency (boto3 comes from the Lambda runtime), dev group with `pytest`, `pytest-mock`, `moto`, `ruff`
- [x] T003 [P] Configure ruff format and lint rules in `pyproject.toml`, targeting py312
- [x] T004 [P] Configure pytest in `pyproject.toml`: testpaths, markers `unit`/`contract`/`integration`, and `--strict-markers`
- [x] T005 Create the Terraform skeleton in `infra/terraform/`: `providers.tf` (AWS ~>5.0, region eu-central-1), `versions.tf` (Terraform >=1.10, required for S3-native state locking), `variables.tf`, `outputs.tf`, and `backend.tf` with the S3 backend using `use_lockfile = true` — no DynamoDB lock table
- [x] T005a Declare `aws_account_id` as a variable in `infra/terraform/variables.tf` and add an `allowed_account_ids` guard to the provider, so an apply against the wrong account fails rather than succeeding. Put the value in an untracked `infra/terraform/terraform.tfvars` and add it to `.gitignore`
- [x] T005b Bootstrap the state bucket outside Terraform, since Terraform cannot create the bucket it is already using as a backend: `aws s3api create-bucket --bucket <state-bucket> --region eu-central-1 --create-bucket-configuration LocationConstraint=eu-central-1`, then enable versioning on it. Record the command in `README.md` so the step is reproducible
- [x] T006 [P] Create `.github/workflows/pr.yml`: ruff format check, ruff lint, `terraform fmt -check`, `terraform validate`, `pytest tests/unit tests/contract`, and a Lambda package build
- [x] T007 [P] Create `.github/workflows/main.yml`: OIDC assume-role (no long-lived keys), build, `terraform apply`, deploy Lambdas, then a smoke test against the deployed tool endpoints
- [x] T008 Create the GitHub OIDC provider and deploy role in `infra/terraform/oidc.tf`, with the trust policy scoped to `repo:sparenicogan/elevenlabs-agent:*`
- [x] T008a Run the first `terraform apply` locally under your own AWS credentials (`aws sso login`, then `terraform init && terraform apply` in `infra/terraform/`). This creates the OIDC provider and deploy role from T008; only after it succeeds do the workflows in T006 and T007 have a role to assume. Done once, never repeated
- [x] T009 [P] Write `.gitignore` and a `README.md` stub covering local setup, and add a `Makefile` with `fmt`, `lint`, `test`, `deploy`, `seed` targets

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Storage, security, configuration, and the cross-cutting mechanisms every story depends on.

**⚠️ CRITICAL**: No user story work begins until this phase is complete.

### Infrastructure

- [x] T010 [P] Define the customer-managed KMS key and alias in `infra/terraform/kms.tf`, with a key policy allowing only the identity-reading Lambda roles
- [x] T011 [P] Define the `customer_identity` table in `infra/terraform/dynamodb.tf` per data-model.md: PK `customer_id`, GSI `phone-index` projecting only `customer_id` and `preferred_language`, SSE with the CMK
- [x] T012 [P] Define the `ledger` table in `infra/terraform/dynamodb.tf`: PK `customer_id`, SK `entry_id`, GSI `status-index` on `customer_id` + `status`
- [x] T013 [P] Define the `conversations` and `customer_summaries` tables in `infra/terraform/dynamodb.tf`, with a `customer-index` GSI on `conversations` for risk-signal aggregation
- [x] T014 [P] Define the transcripts bucket in `infra/terraform/s3.tf`: SSE-KMS, public access blocked, lifecycle expiration driven by `var.transcript_retention_days`
- [x] T015 [P] Define Secrets Manager entries in `infra/terraform/secrets.tf` for the ElevenLabs webhook HMAC secret, the tool API key, and the HubSpot private-app token — values supplied out of band, never in the repo
- [x] T016 [P] Define the eleven SSM policy parameters from research.md D9 in `infra/terraform/ssm.tf`
- [~] T017 (golden-path routes done: verify-identity, get-account-context, match-payment, propose-allocation; credit and escalation land with their handlers) Define the HTTP API, routes, and stage in `infra/terraform/api_gateway.tf`, with a 5-second integration timeout — API, stage and access logging done; **routes deferred to Phase 3**, since a route needs an integration and an integration needs a handler
- [x] T018 Define the reusable Lambda module in `infra/terraform/modules/lambda/`: Python 3.12, 4-second timeout, per-function least-privilege IAM role and log group. Written and validating standalone; **first instantiated in T038**. Audit log-group retention lives in `logs.tf` (T019), not the module — it is one group for the project, not one per function
- [x] T019 Define CloudWatch log groups in `infra/terraform/logs.tf`, including the dedicated `/voice-agent/audit` group

### Cross-cutting code

- [x] T020 [P] Implement structured JSON logging in `src/common/logging.py`, correlated by `conversation_id` and `customer_id`, with a field allowlist that makes logging a raw identity field impossible rather than merely discouraged (FR-029)
- [x] T021 [P] Implement the audit event emitter in `src/common/audit.py`, writing `event_type: AUDIT` records carrying all eleven FR-041 fields
- [x] T022 [P] Implement API-key authentication and request validation in `src/common/auth.py` and `src/common/validation.py`, rejecting unauthenticated or malformed tool calls server-side (FR-046)
- [x] T023 [P] Implement the idempotency helper in `src/common/idempotency.py`: conditional-write wrapper on natural keys that returns the original result on a duplicate rather than an error (FR-022, research D6)
- [x] T024 [P] Implement error categorisation in `src/adapters/errors.py`, mapping every external failure to `TIMEOUT | DEPENDENCY_DOWN | VALIDATION | NOT_AUTHORIZED | INTERNAL` and producing the common error envelope from contracts/tools.md
- [x] T025 [P] Implement the DynamoDB adapter in `src/adapters/dynamo.py`: get, query, conditional put/update, with one bounded retry on reads only and none on writes (FR-023)
- [x] T026 [P] Implement the S3 adapter in `src/adapters/s3.py` for transcript put and key construction
- [x] T027 [P] Implement the SSM and Secrets adapters in `src/adapters/ssm.py` and `src/adapters/secrets.py`, caching at cold start (research D9)
- [x] T028 [P] Implement the HubSpot adapter in `src/adapters/hubspot.py`: contacts, companies, tickets, and engagements via REST v3, with a field allowlist that blocks date of birth, verification answers, and payment detail at the adapter boundary (FR-028)
- [x] T029 Implement the typed policy object in `src/domain/policy.py`, loaded from SSM, so no threshold literal appears in any handler or prompt (FR-045)
- [x] T030 Implement conversation state in `src/common/conversation_state.py`: create the `conversations` item on initiation and record verification status against it, so every tool can check verification server-side rather than trusting the prompt (FR-002)
- [x] T031 Write the Lambda packaging script in `scripts/build.py` and wire it into both workflows

**Checkpoint**: Infrastructure deploys, one endpoint answers, nothing business-specific exists yet.

---

## Phase 3: User Story 1 — Disputed overdue invoice (Priority: P1) 🎯 MVP

**Goal**: The golden path end to end — verify, load context, retrieve the invoice, match the payment server-side, propose the allocation for human review, ticket it, and log it.

**Independent Test**: Call as Meier Bau AG, complete verification, supply the seeded payment details; the payment ends `UNDER_REVIEW`, a HubSpot ticket carries the structured context, and the interaction is logged.

**Note**: This phase builds `verify_identity` only as far as the golden path needs it — three factors and the disclosure gate. The adversarial behaviour (lockout, risk signals, the non-document rule) belongs to US2 and is deliberately not built here.

### Tests for User Story 1

- [x] T032 [P] [US1] Write unit tests for exact payment matching in `tests/unit/test_payment_match.py`: exact amount and exact execution date give MATCH; a one-day or one-franc difference gives NO_MATCH; a missing field or two candidate payments give INSUFFICIENT (FR-010a)
- [x] T033 [P] [US1] Write unit tests for allocation legality in `tests/unit/test_allocation.py`: `UNALLOCATED → UNDER_REVIEW` only, never straight to `ALLOCATED`, and a second proposal returns the first ticket (FR-012, FR-022)
- [x] T034 [P] [US1] Write contract tests in `tests/contract/test_tool_contracts.py` asserting every tool response matches contracts/tools.md, including that `match_payment` never returns a stored amount, date, reference, or address
- [x] T035 [P] [US1] Write the golden-path integration test in `tests/integration/test_golden_path.py` against deployed endpoints

### Implementation for User Story 1

- [x] T036 [P] [US1] Write the synthetic seed script in `scripts/seed/seed.py` and fixtures in `scripts/seed/fixtures.py` for the four customers in quickstart.md — Meier Bau AG carries the overdue CHF 4,200 invoice and the unallocated CHF 4,200 payment with no reference and a mismatched payer address
- [x] T037 [US1] Implement factor checking in `src/domain/verification.py`: compare supplied factors against stored values, count confirmed factors, and return the status enum without ever revealing which factor failed (FR-004)
- [x] T038 [US1] Implement `src/handlers/verify_identity.py` per contracts/tools.md, resolving the customer from the factors themselves and never from the caller-id candidate (research D3), and recording verification state via T030
- [x] T039 [US1] Implement `src/handlers/get_account_context.py`: refuse unless the conversation is VERIFIED, then fetch open invoices from the `status-index`, open and past escalations, CRM data, and the bounded summary (FR-001, FR-039b)
- [x] T040 [US1] Implement exact matching in `src/domain/payment_match.py`, comparing against the payment's execution date and returning MATCH / NO_MATCH / INSUFFICIENT plus an `address_discrepancy` boolean (FR-010a, FR-010b)
- [x] T041 [US1] Implement `src/handlers/match_payment.py`, returning only booleans and identifiers — never the stored values — and never MATCH on an unsettled payment record (FR-010, FR-010d)
- [x] T042 [US1] Implement state-transition rules in `src/domain/allocation.py`, including the authority check that sends every allocation to human review
- [x] T043 [US1] Implement `src/handlers/propose_allocation.py`: conditional write on `status = UNALLOCATED`, audit event with previous and new state, HubSpot ticket, interaction log, and `ALREADY_UNDER_REVIEW` on a duplicate (FR-012, FR-022, FR-041, FR-044)
- [x] T044 [US1] Write the English system prompt in `agent/prompt/en.md`: the disclosure gate, asking for exact amount and exact transfer date, offering to wait while the caller checks their banking app, explaining that a person validates the allocation, and speaking a short acknowledgement before every tool call (FR-023a)
- [x] T045 [US1] Write the tool definitions in `agent/tools.json` matching contracts/tools.md exactly, and the agent configuration in `agent/agent.json` (voice, language detection, transfer)
- [x] T046 [US1] Wire the deployed endpoints, the tool API key secret, and the inbound number into the ElevenLabs agent using `agent/agent.json` and `agent/tools.json`, then run the golden path live

**Checkpoint**: The demo exists. Everything after this makes it safe, multilingual, and reviewable.

---

## Phase 4: User Story 2 — Identity verification gate (Priority: P2)

**Goal**: The gate holds against a caller who asserts, pressures, and retries.

**Independent Test**: Attempt to extract financial information at every point before verification; confirm each of VERIFIED, PARTIALLY_VERIFIED, FAILED, LOCKED behaves as specified.

### Tests for User Story 2

- [ ] T047 [P] [US2] Write unit tests in `tests/unit/test_verification.py`: three factors required; three invoice-printed factors do not verify; PARTIALLY_VERIFIED permits nothing financial; attempts past the limit return LOCKED (FR-003, FR-003a, FR-006)
- [ ] T048 [P] [US2] Write disclosure-gate tests in `tests/contract/test_disclosure_gate.py` asserting every financial tool returns `NOT_AUTHORIZED` when the conversation is not VERIFIED

### Implementation for User Story 2

- [ ] T049 [US2] Add the non-document rule to `src/domain/verification.py`: at least one confirmed factor must be email, phone, date of birth, or account opening year, so possession of an invoice is never sufficient (FR-003a)
- [ ] T050 [US2] Add attempt counting and lockout to `src/handlers/verify_identity.py`: increment `failed_verification_attempts`, set `locked_until` past the configured limit, reset on success (FR-006)
- [ ] T050a [US2] Count distinct values offered per factor per conversation in `src/domain/verification.py` and `src/common/conversation_state.py`, storing salted hashes rather than values; a third distinct value for one factor raises a risk signal and escalates (FR-006a–d)
- [ ] T050b [P] [US2] Write tests in `tests/unit/test_guessing.py`: one self-correction proceeds, a third distinct value for the same factor escalates, the allowance is per factor, and no attempted value is stored in readable form
- [ ] T051 [US2] Implement risk-signal writing in `src/domain/risk.py` and record a signal on lockout and on a candidate-id mismatch (FR-016, research D3)
- [ ] T052 [US2] Add `next_factor_hint` selection to `src/handlers/verify_identity.py`, naming a field to ask for and never carrying a value (FR-004)
- [ ] T053 [US2] Extend `agent/prompt/en.md` with guidance for a caller who cannot find the information — naming which document carries it, without revealing the value (FR-005) — and with escalation on LOCKED
- [ ] T053a [US2] Add the unverified-escalation branch to `agent/prompt/en.md`: when identity cannot be established, ask what the caller is calling about, record their answer verbatim, say only that identity cannot be confirmed, and transfer with that context (FR-019b, FR-019f)
- [ ] T053b [US2] Accept `IDENTITY_NOT_ESTABLISHED` from an unverified conversation in `src/handlers/create_escalation.py`, creating a ticket with no contact or company association and a handoff carrying the stated problem, unverified self-description, verification outcome, attempt count and language — and no account fact (FR-019c, FR-019d)
- [ ] T053c [P] [US2] Write tests in `tests/contract/test_unverified_escalation.py`: the escalation succeeds without verification, the ticket has no associations, the handoff contains no invoice, payment, balance or account field, and caller text is recorded rather than interpreted (FR-019e)

---

## Phase 5: User Story 3 — Autonomous small credit (Priority: P3)

**Goal**: A clean, verified customer gets a small goodwill credit on the call, with an audit trail.

**Independent Test**: Request CHF 40 against a named charge as Rossi; it is granted on the call and appears as a ledger entry with an audit event and a CRM log.

### Tests for User Story 3

- [ ] T054 [P] [US3] Write unit tests in `tests/unit/test_credit.py` covering every boundary: CHF 100 with CHF 400 of history granted at exactly CHF 500; CHF 100 with CHF 401 refused; CHF 101 always refused; CHF 100 against a CHF 20 charge refused as `DENIED_EXCEEDS_ENTRY` (FR-013b, FR-013d)
- [ ] T055 [P] [US3] Write eligibility tests in `tests/unit/test_credit_eligibility.py`: inactive account, missing entry, and open dispute each refuse (FR-013)

### Implementation for User Story 3

- [ ] T056 [US3] Implement `src/domain/credit.py`: eligibility checks, then the entry-amount cap, then the two inclusive ceilings — in that order, so the most specific denial reason is the one reported (FR-013, FR-013b, FR-013d)
- [ ] T057 [US3] Implement the rolling 12-month total in `src/domain/credit.py`, summing credit entries from the `ledger` inside the window
- [ ] T058 [US3] Implement `src/handlers/request_credit.py`: evaluate and issue in one operation so evaluation cannot be skipped, write the credit entry linked to the named charge, emit the audit event, log to the CRM, idempotent on `conversation_id + entry_id + amount` (research D1, FR-013a)
- [ ] T059 [US3] Extend `agent/prompt/en.md` with the credit flow: identify the specific charge first, state the amount and effect on grant, and escalate rather than negotiate on any refusal

---

## Phase 6: User Story 4 — Threshold splitting (Priority: P4)

**Goal**: Repeated small credits inside the window escalate instead of being granted, without accusing the caller.

**Independent Test**: As Dubois with CHF 460 of history, request CHF 80; it is refused, a risk signal is written, and an escalation is created.

### Tests for User Story 4

- [ ] T060 [P] [US4] Write unit tests in `tests/unit/test_risk.py`: the cumulative rule triggers at CHF 540; a high-risk result overrides passing eligibility (FR-014, FR-015)
- [ ] T061 [P] [US4] Write a wording test in `tests/unit/test_refusal_language.py` asserting the refusal carries a neutral reason and no accusation of fraud

### Implementation for User Story 4

- [ ] T062 [US4] Extend `src/domain/risk.py` with the remaining signals from FR-016: high contact frequency, repeated disputes, repeated failed verification, conflicting identity data, unusual payment behaviour — each aggregated from `conversations` via the `customer-index`
- [ ] T063 [US4] Implement the risk override in `src/domain/credit.py` so any high-risk result forces `DENIED_RISK` and escalation regardless of eligibility (FR-015)
- [ ] T064 [US4] Extend `agent/prompt/en.md` so a refusal on risk grounds says the request needs review, gives a neutral reason, and makes no accusation

---

## Phase 7: User Story 5 — Degradation, escalation, transfer failure (Priority: P5)

**Goal**: Every dependency failure produces a caller-safe outcome with a next step, and no failure is ever spoken as an answer.

**Independent Test**: `pytest tests/unit/test_failure_scenarios.py` — eight scenarios, each asserting a safe outcome and no fabricated financial statement.

### Tests for User Story 5

- [ ] T065 [P] [US5] Write `tests/unit/test_failure_scenarios.py` covering all eight required scenarios with the adapter layer patched: identity store down, invoice store down, payment lookup down, CRM down, transfer failure, duplicate post-call webhook, partial failure after a mutation, and a slow dependency hitting the 4-second timeout (SC-005, research D8)
- [ ] T066 [P] [US5] Write handoff composition tests in `tests/unit/test_handoff.py` asserting every FR-019 field is present
- [ ] T067 [P] [US5] Write idempotency tests in `tests/unit/test_idempotency.py`: replaying an allocation, a credit, an escalation, and a post-call notification each create nothing twice (SC-007)

### Implementation for User Story 5

- [ ] T068 [US5] Implement `src/domain/handoff.py`, composing the structured handoff server-side from verified identity state, intent, facts gathered, actions attempted and their outcomes, and the escalation reason (FR-019, research D2)
- [ ] T069 [US5] Implement `src/handlers/create_escalation.py`: create or append via `existing_ticket_id`, return the handoff summary, and return `CRM_UNAVAILABLE_PERSISTED` rather than losing the escalation when HubSpot is down (FR-025, FR-031b)
- [ ] T070 [US5] Implement callback persistence in `src/handlers/create_escalation.py` for the transfer-failure path (FR-020)
- [ ] T071 [US5] Verify whether the ElevenLabs transfer tool returns control to the agent on a failed dial (research D4), and record the finding in research.md; wire in-call recovery if it does, and state the limitation plainly in the README if it does not
- [ ] T072 [US5] Extend `agent/prompt/en.md` with the degradation rules: distinguish "cannot tell you right now" from a factual negative, never speak UNKNOWN or SERVICE_UNAVAILABLE as an outcome, always close with a next step (FR-011, FR-026)
- [ ] T073 [US5] Configure the transfer destination and the escalation triggers from FR-018 in `agent/agent.json`

---

## Phase 8: User Story 6 — Multilingual and adaptive (Priority: P6)

**Goal**: German, French, Italian, and English, held for the whole call, with the greeting chosen from the inbound number.

**Independent Test**: Run the golden path in each language; no turn falls out of the caller's language, and Rossi's call from a known number opens in Italian without the agent using a name.

### Tests for User Story 6

- [ ] T074 [P] [US6] Write tests in `tests/unit/test_conversation_init.py`: a known number yields its stored language, an unknown number yields German, and the response never carries a name or any account fact (FR-033a, FR-033b)
- [ ] T075 [P] [US6] Write locale rendering tests in `tests/unit/test_locale.py` asserting amounts and dates follow convention while staying numerically exact (FR-037)

### Implementation for User Story 6

- [ ] T076 [US6] Implement `src/handlers/conversation_init.py` per contracts/webhooks.md: resolve `caller_id` via the `phone-index`, return the candidate id as a `secret__` dynamic variable and the greeting language, and nothing else
- [ ] T077 [P] [US6] Write `agent/prompt/de.md` as the full translation of the English prompt, not a summary of it
- [ ] T078 [P] [US6] Write `agent/prompt/fr.md`
- [ ] T079 [P] [US6] Write `agent/prompt/it.md`
- [ ] T080 [US6] Enable the language detection system tool for DE/FR/IT/EN in `agent/agent.json` and require every tool-driven statement and escalation message to stay in the caller's language (FR-033)
- [ ] T081 [US6] Add the adaptation rules to all four prompts in `agent/prompt/`: adjust pace, sentence length, vocabulary, and confirmation frequency from the observed conversation only, never from assumptions about the person, and offer to slow down, simplify, or switch (FR-034, FR-035)
- [ ] T082 [US6] Persist `preferred_language` and `communication_style` to `customer_identity` from `src/handlers/post_call.py` when the caller switches language or asks for a different style (FR-036)

---

## Phase 9: User Story 7 — Memory, audit, and metrics (Priority: P7)

**Goal**: Every call leaves a defensible trail, and the next call starts from it without repeating the caller.

**Independent Test**: Two calls for the same customer; the second loads the first's summary *and* fetches invoices, escalations, and CRM data live; replaying the webhook creates nothing twice.

### Tests for User Story 7

- [ ] T083 [P] [US7] Write tests in `tests/unit/test_post_call.py`: HMAC validation including timestamp window, duplicate delivery creating nothing twice, and a failure in one step not rolling back earlier steps (FR-024, FR-025)
- [ ] T084 [P] [US7] Write summary tests in `tests/unit/test_summary.py`: stays within `summary_max_chars` across ten successive regenerations, and contains no verification answer, date of birth, or payment credential (SC-008, FR-029)
- [ ] T085 [P] [US7] Write metrics tests in `tests/unit/test_metrics.py` asserting all nine derived rates in FR-043 can be computed from stored fields

### Implementation for User Story 7

- [ ] T086 [US7] Implement HMAC validation in `src/handlers/post_call.py` per contracts/webhooks.md, rejecting a timestamp outside the 30-minute window
- [ ] T087 [US7] Implement transcript persistence to S3 in `src/handlers/post_call.py` at the key layout in data-model.md
- [ ] T088 [US7] Implement metrics persistence to `conversations` in `src/handlers/post_call.py`, writing every FR-042 field
- [ ] T089 [US7] Implement summary regeneration in `src/handlers/post_call.py`: summarise the prior summary plus this call, cap at `summary_max_chars`, write with a version check, and record provenance (FR-039a)
- [ ] T090 [US7] Implement transfer reconciliation in `src/handlers/post_call.py` as the backstop for a failed transfer that ended the call (FR-020, research D4)
- [ ] T091 [US7] Write the metrics derivation queries in `scripts/metrics.py` as CloudWatch Logs Insights and DynamoDB queries producing the nine rates (FR-043)

---

## Phase 10: User Story 8 — Address discrepancy (Priority: P8)

**Goal**: The mismatch is raised with the caller after the payment is confirmed, their explanation is recorded on the existing ticket, and a human corrects it.

**Independent Test**: Run the golden path; the discrepancy is raised only after MATCH, the answer lands on the same ticket, and no address field is written.

### Tests for User Story 8

- [ ] T092 [P] [US8] Write tests in `tests/unit/test_discrepancy.py`: the flag is not exposed before a MATCH, the explanation appends to the existing ticket rather than creating a second, and no address write occurs (FR-031, FR-031a, FR-031b)

### Implementation for User Story 8

- [ ] T093 [US8] Accept the `discrepancy` field in `src/handlers/create_escalation.py` and append it to the ticket named by `existing_ticket_id`
- [ ] T094 [US8] Add the discrepancy step to every prompt in `agent/prompt/`: only after the payment is confirmed, ask whether the company moved or the record has a typo, say a person will correct it, and never offer or confirm either address value

---

## Phase 11: Polish & Cross-Cutting

- [ ] T095 [P] Verify no identity field reaches any log by asserting the allowlist in `tests/unit/test_log_scrubbing.py` and reviewing a real call's log output (FR-029, SC-002)
- [ ] T096 [P] Verify the `src/adapters/hubspot.py` field allowlist against a real ticket and interaction record — no date of birth, verification answer, or payment detail (FR-028, SC-002)
- [ ] T097 [P] Tighten per-Lambda IAM in `infra/terraform/iam.tf` so only `verify_identity` and `conversation_init` can read `customer_identity` (Principle X)
- [ ] T098 [P] Verify the lifecycle rules in `infra/terraform/s3.tf` and `infra/terraform/logs.tf` match FR-038a: transcripts 90 days, metadata and metrics 10 years, audit 10 years
- [ ] T099 Run the full quickstart.md validation for all eight user stories and fix what it surfaces
- [ ] T100 Write `README.md`: architecture, the trust boundary between agent and backend, how to run the tests, and an honest statement of what is out of scope (SMS, payment links, auto-reconciliation) and what is proven by tests rather than demonstrated live
- [ ] T101 Record the 3–5 minute Loom: the golden path plus one failure path, using the pre-recording checklist in quickstart.md

---

## Dependencies

**Phase order**: Setup → Foundational → US1 → US2 → US3 → US4 → US5 → US6 → US7 → US8 → Polish.

**Story dependencies**:

- **US1** depends on Foundational only. It builds a minimal `verify_identity` so the golden path can run.
- **US2** extends US1's verification handler. Not independent of US1 in code, though independently testable in behaviour.
- **US3** depends on Foundational; independent of US1 and US2 in code.
- **US4** depends on US3 (`credit.py`) and on US2's `risk.py`.
- **US5** depends on US1 for something to escalate about.
- **US6** depends on US1's prompt existing to translate.
- **US7** depends on US1 for a call worth persisting.
- **US8** depends on US1 (`match_payment`) and US5 (`create_escalation`).

## Parallel Opportunities

- **Phase 1**: T003, T004, T006–T009 run together.
- **Phase 2**: T010–T016 (separate Terraform files) and T020–T028 (separate modules) are two large parallel batches.
- **Within each story**: all test tasks marked [P] run together before implementation.
- **Phase 8**: T077, T078, T079 — the three translations are fully independent.
- **Phase 11**: T095–T098 run together.

## Implementation Strategy

**MVP is Phase 3 (US1).** Setup, Foundational, and US1 give a working golden path: verified caller, server-side payment match, allocation proposed for human review, ticket, CRM log. That is the demo. Everything after it makes the system defensible rather than demonstrable.

**Highest-risk task**: T071. If the transfer tool terminates the call on a failed dial, FR-020's promise of telling the caller cannot be met on that path, and the README and the Loom must say so rather than imply otherwise. Do it before building the rest of US5.

**Watch the budget.** The spec targets roughly 20 hours. If time runs short, US8 and parts of US7 are the intended casualties, per Constitution IX — never US2 or US5, which are what the take-home is actually assessing.

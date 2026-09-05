# Implementation Log — Phases 1 and 2

**Scope**: tasks T001–T031 (33 including sub-tasks), covering repository scaffolding, the AWS
bootstrap, and the storage, security and cross-cutting foundation.

**Purpose**: what was built, how, and why — including the decisions that departed from the task
text and the four failures encountered on the way. A reviewer should be able to read this instead
of reconstructing intent from a diff.

Companion documents: [spec.md](../specs/001-voice-billing-agent/spec.md) for requirements,
[plan.md](../specs/001-voice-billing-agent/plan.md) for architecture,
[research.md](../specs/001-voice-billing-agent/research.md) for the design decisions this log
implements.

---

## Phase 1 — Setup

Goal: a repository that lints, tests and deploys, before any business logic exists. Nothing in
this phase knows what an invoice is.

### T001 — Directory structure

**What**: `src/{domain,adapters,handlers,common}/`, `agent/prompt/`, `infra/terraform/`,
`scripts/seed/`, `tests/{unit,contract,integration}/`, each a Python package.

**How**: `mkdir -p` plus `__init__.py` where imports need to resolve.

**Why this shape**: the `domain / adapters / handlers` split is the load-bearing architectural
decision of the whole project, not a stylistic one.

- `domain/` holds pure functions — no network, no AWS, no clock where avoidable. Constitution
  Principle II says financial rules run server-side independently of the LLM; putting them in a
  layer that *cannot* reach the network makes that claim checkable rather than aspirational.
- `adapters/` is the only code that touches anything external. That boundary is what lets a test
  substitute a failing DynamoDB without a runtime fault-injection flag existing in production code
  — which matters because the clarification session chose test-only failure coverage (research D8).
- `handlers/` stay thin: parse, authorise, call domain, log. Thin enough that reviewing one for
  "does this check verification first" takes seconds.

### T002 — Python project

**What**: `pyproject.toml` with Python 3.12, `httpx` as the single runtime dependency, dev group of
pytest, pytest-mock, moto, ruff.

**How**: standard `[project]` table; `uv` for resolution and locking.

**Why one runtime dependency**: boto3 is provided by the Lambda runtime. Bundling it roughly
triples the package and the cold start, and the cold start eats into a 5-second tool budget
(FR-023). `httpx` is vendored only because HubSpot is the one external HTTP call.

### T003 / T004 — ruff and pytest configuration

**What**: ruff formatting and lint (`E`, `F`, `I`, `UP`, `B`, `SIM`) at 100 columns; pytest with
`unit` / `contract` / `integration` markers and `--strict-markers`.

**Why `--strict-markers`**: a typo in a marker silently runs nothing. In a project where the test
suite is the only evidence for a third of the spec (SC-005), a test that quietly never runs is
worse than one that fails.

**Deviation**: `.specify` is excluded from ruff. It contains vendored Spec Kit tooling that fails
the lint rules. Reformatting someone else's tool is not a change that traces to any requirement.

### T005 — Terraform skeleton

**What**: `versions.tf`, `backend.tf`, `providers.tf`, `variables.tf`, `outputs.tf`.

**How**: Terraform `>= 1.10`, AWS provider `~> 5.0`, S3 backend with `use_lockfile = true`.

**Why 1.10 rather than the 1.6 originally planned**: 1.10 introduced S3-native state locking.
Before it, locking required a DynamoDB table that existed for no reason other than Terraform's own
history. Raising the floor removes a resource that would have needed provisioning, IAM, and
explaining.

### T005a — Account guard

**What**: `aws_account_id` variable, `allowed_account_ids` on the provider, value in an untracked
`terraform.tfvars`.

**Why**: the environment has an org SSO session that can reach several accounts, and a shell's
`AWS_PROFILE` is easy to get wrong. Without the guard, an apply against the wrong account succeeds
quietly. With it, it fails immediately. The account id stays out of the repository — not a
credential, but nothing is gained by publishing it.

### T005b — State bucket bootstrap

**What**: `<state-bucket>`, created with the AWS CLI, versioning enabled.

**Why outside Terraform**: Terraform cannot create the bucket it is already configured to use as
its backend. Versioning is the undo button if a state file is corrupted — cheap, and the only
recovery path that exists.

### T006 / T007 — CI workflows

**What**: `pr.yml` (ruff format check, ruff lint, pytest, package build; terraform fmt, init
`-backend=false`, validate) and `main.yml` (OIDC assume-role, build, terraform apply).

**Why `-backend=false` in the PR workflow**: validation then needs no AWS credentials and no state
bucket. A pull request from a fork, or before bootstrap, still gets checked.

**Deviation, later corrected**: both workflows initially omitted the Lambda build step, because
`scripts/build.py` did not exist yet and a workflow that fails on its first run teaches nothing.
The step was added to both once T031 landed. `main.yml` needs it *before* `terraform apply`,
because the Lambda module reads the package with `filebase64sha256`, which fails at plan time if
the artefact is absent.

### T008 / T008a — OIDC deploy role and first apply

**What**: GitHub OIDC provider, a deploy role trusted only by this repository, PowerUserAccess plus
a narrow inline IAM policy. Then the first `terraform apply`, run locally.

**Why PowerUserAccess plus a prefix-scoped inline policy**: PowerUserAccess covers every service
this project provisions and grants no IAM at all, so a compromised workflow cannot escalate its own
permissions. The project genuinely needs to manage its own Lambda roles, so a second policy grants
exactly that, confined to `voice-agent-*` role and policy names.

**Why the first apply is local**: the role CI would assume did not exist yet. This is a
chicken-and-egg with exactly one solution, and it happens once.

### T009 — gitignore, Makefile, README

**What**: standard Python and Terraform ignores plus `terraform.tfvars`; Make targets for `fmt`,
`lint`, `test`, `deploy`, `seed`, `bootstrap`; a README covering setup and bootstrap.

**Added later**: `AWS_PROFILE ?= voice-agent-admin` exported from the Makefile. The shell profile
in this environment sets a global `AWS_PROFILE` pointing at a different account, and a global
default means whichever project's export lands last silently becomes the default for all the
others. Setting it in the Makefile makes `make deploy` correct on a fresh clone regardless of the
shell, while `?=` still allows an override.

---

## Phase 2 — Foundational

Goal: storage, security and the cross-cutting mechanisms every user story depends on. Still nothing
that knows what an invoice is — but everything that will keep the invoice logic honest.

### T010 — KMS

**What**: two customer-managed keys, `voice-agent-identity` and `voice-agent-data`, both with
rotation enabled.

**Why two keys rather than one**: this is the decision that makes Principle IV real. A single
shared key would need a policy admitting every Lambda in the project, and the isolation between
identity data and everything else would be nominal. The identity key's policy admits only the two
handlers that legitimately read identity data — `verify_identity` and `conversation_init` — matched
on role-name prefix with `ArnLike`, so the roles need not exist when the key is created.

**Cost of the choice**: two keys at roughly $1/month each rather than one. Trivially worth it.

### T011–T013 — DynamoDB

**What**: four tables — `customer-identity`, `ledger`, `conversations`, `customer-summaries` — all
PAY_PER_REQUEST, all encrypted, identity and ledger with point-in-time recovery.

**Key design notes**:

- `ledger` is one table for invoices, payments, credit notes and adjustments, keyed
  `customer_id` + `entry_id`. Balances are derived from entries and never stored (FR-017): a stored
  balance is a second source of truth that can disagree with the entries that produced it.
- `phone-index` on `customer-identity` projects **only** `customer_id` and `preferred_language`.
  The conversation initiation webhook needs a greeting language and nothing else, and a projection
  is a permission boundary: a compromised query against this index cannot return identity data
  because the index does not contain it.
- `status-index` on `ledger` exists because open invoices and unallocated payments are read by
  status on every call. Without it those reads are table scans.
- `customer-index` on `conversations` supports risk-signal aggregation over a customer's recent
  calls, which is why risk signals live inside conversation records rather than in a fifth table.

### T014 — S3 transcripts

**What**: private bucket, SSE-KMS under the data key, versioning on, lifecycle expiring
`customers/` objects at 90 days and non-current versions after one day.

**Why the non-current version rule**: versioning is on for recovery, but without expiring
non-current versions a "deleted" transcript outlives its retention period indefinitely. The
retention promise would be false.

**Why the lifecycle rather than application deletion**: FR-038a requires expiry enforced by the
storage layer. Code that must remember to delete eventually forgets.

### T015 — Secrets

**What**: three Secrets Manager entries, created **empty**: the ElevenLabs webhook HMAC secret, the
tool API key, the HubSpot token.

**Why empty**: a value passed through Terraform lands in state and in plan output. Creating the
container in Terraform and populating it out of band with `put-secret-value` keeps the value out of
the repository, out of state, and out of any CI log.

### T016 — Policy parameters

**What**: eleven SSM parameters under `/voice-agent/policy/`, with `ignore_changes = [value]`.

**Why**: Principle VIII. Every threshold in the system — factor count, credit ceilings, the rolling
window, timeouts, retention, the summary cap — is a parameter, so tuning one is a parameter edit
rather than a prompt rewrite or a redeploy.

**Why `ignore_changes`**: these are operational settings a human may tune in the console during an
incident. Terraform reverting a deliberate change on the next apply would be the opposite of
useful.

### T017 — API Gateway *(partial)*

**What**: HTTP API, `$default` stage with auto-deploy, throttling at 10 rps / 20 burst, JSON access
logging.

**Why the access log format omits bodies**: a tool request body carries caller-supplied identity
factors. Logging bodies would put verification answers in CloudWatch, which FR-029 forbids.

**Why still partial**: routes need integrations, integrations need Lambda functions, and there are
none yet. Routes land in Phase 3 alongside the handlers they serve. Marked `[~]` in tasks.md rather
than ticked, because a task list that lies is worse than one that is behind.

### T018 — Lambda module

**What**: a reusable module at `infra/terraform/modules/lambda/` — function, its own IAM role, its
own log group, 4-second timeout.

**Why per-function roles**: Principle X and T097. Read access to the identity table must be
grantable to two functions and deniable to the other six. A shared execution role makes that
impossible.

**Why a 4-second timeout when the agent's budget is 5**: the backend must fail *before* the agent
gives up, so the agent receives a structured error it can speak rather than an opaque timeout it
might paper over (research D5). The one-second margin covers API Gateway overhead.

**Deviation**: the task specified `infra/terraform/lambda.tf` and asked for the audit log group in
the module. It is a module instead, because it is instantiated eight times; and the audit group
lives in `logs.tf`, because there is one audit stream for the project, not one per function.

### T019 — Log groups

**What**: `/voice-agent/audit` at 3,653 days (10 years), `/voice-agent/api-access` at 90.

**Why audit is a log group rather than a table**: audit events are append-only and never read by
key during a call. A log group with retention plus Logs Insights covers the review case; a
DynamoDB table would add IAM surface and Terraform for a read pattern that does not exist. If audit
ever needs to be read back mid-call, it moves.

**Why 10 years**: Swiss business-record retention. The clarification session set metadata and
metrics to the same period, so a financial action taken on a call can still be explained a decade
later.

### T020 — Structured logging

**What**: `src/common/logging.py`, JSON to stdout, with a field **allowlist**.

**Why an allowlist rather than a redaction list**: FR-029 forbids verification answers and raw
identity fields in logs. A denylist protects against the fields someone thought of; an allowlist
protects against the ones they did not. A future call site that passes `date_of_birth` in a log
call leaks nothing — the field is dropped before it reaches CloudWatch. The rule stops depending on
every future author remembering it.

### T021 — Audit events

**What**: `src/common/audit.py`, a frozen `AuditEvent` dataclass with all eleven FR-041 fields,
written as `event_type: AUDIT`.

**Why a dataclass rather than a dict**: an audit record missing its authorizing rule or its
previous state cannot be reconstructed later, and by then the call is over. Making the field set a
type means an incomplete event fails at the call site.

**Why it bypasses the logging allowlist**: an audit record's fields are fixed by the dataclass,
contain no caller speech and no identity attributes, and must not be silently trimmed. The bypass
is deliberate and commented.

### T022 — Auth and validation

**What**: `src/common/auth.py` (API key check, webhook HMAC validation) and
`src/common/validation.py` (required fields, money, dates).

**Design notes**:

- Both comparisons use `hmac.compare_digest`. A timing difference on an API key check leaks the key
  one byte at a time to anyone who can call the endpoint.
- The webhook signature check enforces a 30-minute timestamp window, so a captured request that is
  still cryptographically valid cannot be replayed indefinitely.
- Money is parsed as `Decimal`, never `float`. 4200.00 has no exact binary representation, and
  FR-010a requires *exact* amount matching. A float comparison would fail a correct caller.
- Dates are strict ISO. "Last Tuesday" must be resolved by the agent before the tool is called; the
  backend will not guess at a date that determines whether a payment matches.

### T023 — Idempotency

**What**: `src/common/idempotency.py` (deterministic key construction) plus the conditional-write
primitives in the DynamoDB adapter.

**Why conditional writes rather than an idempotency table**: the natural keys are already unique.
A conditional write is atomic and needs no second table; a duplicate simply fails the condition,
and the handler returns the original result rather than an error — so a retry is indistinguishable
from a first call to the person on the phone.

**Why keys are hashed**: fixed length regardless of how many identifiers an action needs.

### T024 — Error categorisation

**What**: `src/adapters/errors.py` — five categories, a `ToolError` exception, and the structured
envelope from `contracts/tools.md`.

**Why this exists at all**: Principle III turns on one distinction — a dependency being unreachable
is not the same answer as an authoritative "no". Every external failure passes through here, so the
agent always receives a status it can speak safely rather than an exception it might interpret
optimistically.

**Design detail**: only `TIMEOUT` and `DEPENDENCY_DOWN` are retryable. `VALIDATION` and
`NOT_AUTHORIZED` mean the request itself was wrong, and no amount of retrying changes that.
`message_hint` gives the agent caller-safe phrasing that asserts no financial fact — because at the
moment it is returned, the backend does not know one.

### T025 — DynamoDB adapter

**What**: `get`, `query`, `put_if_absent`, `update_if`.

**Design notes**:

- Reads retry once with backoff; writes never retry (FR-023). A write that may have partly
  succeeded must not be repeated.
- A failed read raises rather than returning empty. An empty list must mean "none exist", never
  "could not tell" — conflating them is exactly how an agent ends up telling a caller they have no
  invoices during an outage.
- `put_if_absent` returns `False` rather than raising on a duplicate: a duplicate is a normal
  outcome, not an error.
- `update_if` takes a condition expression, which is what makes an illegal state transition
  impossible even under concurrent calls — `UNALLOCATED → UNDER_REVIEW` can only happen once.

### T026 / T027 — S3, SSM, Secrets adapters

**What**: transcript key construction and put; policy and secret reads, cached per execution
environment.

**Why caching at cold start**: keeps the parameter read off the hot path. A policy change takes
effect within one Lambda lifecycle, which is the intended trade (research D9).

**Why the transcript key is partitioned by customer**: a per-customer deletion request becomes a
prefix delete rather than a search.

**Why a failed policy read raises**: a handler that cannot read its thresholds must refuse, not
assume one. Assuming a credit ceiling is how a system issues a credit it should not have.

### T028 — HubSpot adapter

**What**: ticket creation with contact and company association, note appending, interaction
logging, contact and ticket reads.

**Why the allowlist is enforced here**: FR-028 lists what the CRM may hold. Enforcing it at this
boundary rather than at each call site means a future handler cannot leak a date of birth into a
ticket by passing the wrong dictionary — it is stripped before the request is built.

**Why stripping applies on read as well as write**: HubSpot may hold fields this project never
wrote, added by a person in the UI. Stripping on read means such a field cannot reach a transcript,
a summary, or a log.

**Why `append_note` exists**: FR-031b requires a second finding on one call to attach to the
existing ticket rather than create a second one. The caller should not generate two tickets for one
conversation.

### T029 — Typed policy

**What**: `src/domain/policy.py`, a frozen dataclass loaded from SSM.

**Why typed rather than a raw dict**: a malformed parameter fails loudly at load rather than
silently comparing a string to a number somewhere inside a credit decision. `Decimal` for money,
`int` for counts.

### T030 — Conversation state

**What**: `src/common/conversation_state.py` — `start`, `set_verification`, `require_verified`.

**This is where Principle I lives.** If verification status were held in the prompt, a caller who
talked their way past the model would be verified. Here it is a row in DynamoDB written by the
backend, and every financial tool calls `require_verified()`, which raises `NOT_AUTHORIZED` unless
the stored status is `VERIFIED`. The model's opinion is not consulted.

`PARTIALLY_VERIFIED` permits nothing financial. The clarification session set verification at a
single uniform bar of three factors, so partial means "keep asking", not "proceed carefully".

### T031 — Build script

**What**: `scripts/build.py`, producing `dist/lambda.zip` (571 KB), wired into both workflows.

**Why one package for all eight handlers**: the code is small, and a single artefact means the
functions cannot drift onto different versions of the domain rules — which would be a genuinely
confusing bug.

**Why `uv pip install --target` with an explicit platform**: the project venv is uv-managed and has
no pip. `--python-platform x86_64-manylinux2014 --python-version 3.12` targets the Lambda runtime
rather than the build machine, so a macOS laptop and an Ubuntu runner produce the same artefact.

---

## Failures encountered

Six, all worth recording: three were silent, two were misleading, and one hung rather than
failing. Every one of them was on the deploy path, which is the argument for exercising CI before
there is anything at stake in it.

### 1. CloudWatch Logs could not use the KMS key

**Symptom**: `terraform apply` failed creating `/voice-agent/audit` with "The specified KMS key does
not exist or is not allowed to be used".

**Cause**: CloudWatch Logs encrypts as the **service principal**, not on the caller's behalf. The
data key's default policy admits only the account root, so the service was refused. DynamoDB and S3
applied cleanly because they encrypt under the caller's IAM permissions.

**Fix**: an `AllowCloudWatchLogs` statement granting the Logs service principal encrypt and decrypt,
conditioned on `kms:EncryptionContext:aws:logs:arn` matching this account's log groups — so the
grant cannot be used to decrypt anything else.

### 2. The virtual environment was Python 3.13, Lambda runs 3.12

**Symptom**: none. Everything passed.

**Cause**: `requires-python = ">=3.12"` is a floor, and uv installed the newest available.

**Why it mattered**: the deployment package would have been built against a different minor version
than the one executing it. With pure-Python dependencies this may never surface; with a compiled
one it fails at invoke time, in production, with an import error.

**Fix**: `.python-version` pinned to 3.12, `requires-python` tightened to `>=3.12,<3.13`, and the
build given an explicit platform and version target.

### 3. The deploy workflow had no role to assume

**Symptom**: "Credentials could not be loaded, please check your action inputs".

**Cause**: the `AWS_DEPLOY_ROLE_ARN` repository variable was never set, so `role-to-assume` was
empty. The action does not treat an empty input as an error, so the message points at credentials
rather than at configuration.

**Fix**: set the variable. Worth noting that this is exactly the failure mode a bootstrap step is
prone to: a manual step in a documented sequence that nothing verifies.

### 4. The OIDC trust policy matched nothing

**Symptom**: "Not authorized to perform sts:AssumeRoleWithWebIdentity", with a trust policy that
looked textbook-correct.

**Cause**: this organisation has **immutable OIDC subject claims** enabled, so GitHub presents

```
repo:sparenicogan@299617649/elevenlabs-agent@1354831999:ref:refs/heads/main
```

rather than the documented `repo:<owner>/<repo>:ref:...`. The numeric ids survive a rename, which is
the security point — renaming a repository cannot transfer its AWS trust to whoever claims the freed
name. A `StringLike` pattern written against the documented form matches nothing.

**How it was found**: guessing was unproductive, so a temporary workflow fetched the OIDC token and
printed its claims. The mismatch was then obvious. The workflow was deleted afterwards.

**Fix**: `github_owner_id` and `github_repository_id` variables, and a trust condition built from
the immutable form.

**Why the error message is misleading**: a subject mismatch and a missing permission produce the
same text, so the natural first suspicion — an org SCP — was wrong and cost time.

### 5. Terraform waited 28 minutes on an interactive prompt

**Symptom**: the deploy workflow ran for 28 minutes with no output, then had to be cancelled. It
held the state lock throughout, which blocks every other apply — including a local one.

**Cause**: `terraform.tfvars` is untracked (T005a), so CI had no value for `var.aws_account_id`.
Terraform asked for it on stdin and waited. In a non-interactive runner this looks exactly like a
slow build.

**Fix**: the value comes from a repository variable via `TF_VAR_aws_account_id`, and both workflows
set `TF_INPUT=false` so a missing variable fails in seconds instead of hanging.

**Why the hang is worse than the error**: an error is visible in the run list. A prompt is not — it
consumed half an hour and left a lock that needed `force-unlock` to clear.

**Cost of the account guard, honestly**: the guard from T005a is what created this. It remains
worth keeping, but a variable with no default and an untracked value file needs a CI path, and that
should have been provided when the guard was.

### 6. The deploy role could not read the provider it authenticates through

**Symptom**: `AccessDenied: not authorized to perform iam:GetOpenIDConnectProvider`.

**Cause**: Terraform refreshes `aws_iam_openid_connect_provider` on every apply. PowerUserAccess
grants no IAM at all, and the inline policy covered roles and policies but not the provider. So the
role could authenticate through the provider and then not read it.

**Fix**: read and adjust on that single provider ARN. Deliberately *not* create or delete —
creating it is a bootstrap step, and deleting the provider CI authenticates through would be
self-destructive.

**Generalisation**: a least-privilege deploy role has to cover everything Terraform *refreshes*, not
merely everything it changes. Refresh reads every managed resource on every run.

---

## Deviations register

| Task | Specified | Built | Why |
|---|---|---|---|
| T003 | ruff over the repository | `.specify` excluded | Vendored tooling; reformatting it traces to no requirement |
| T005 | Terraform ≥1.6 | ≥1.10 | S3-native locking removes a DynamoDB table that would exist only for locks |
| T006/T007 | Build step in both workflows | Added once `build.py` existed | A workflow failing on its first run teaches nothing |
| T017 | API, routes and stage | API and stage only | Routes need integrations, which need handlers |
| T018 | `lambda.tf`, audit group in module | `modules/lambda/`, audit group in `logs.tf` | It is instantiated eight times; there is one audit stream, not eight |
| T009 | Makefile targets | Plus `AWS_PROFILE` export | A global profile in the shell pointed at the wrong account |

---

## State at the end of Phase 2

**AWS the target account, eu-central-1** — 39 Terraform-managed resources: 4 DynamoDB tables,
2 KMS keys with aliases, the transcripts bucket with its lifecycle, 3 empty secrets, 11 policy
parameters, the HTTP API and stage, 2 log groups, the OIDC provider and deploy role.

**Repository** — both workflows proven. `pr.yml` green on lint, tests, package build and Terraform
validation. `main.yml` green: OIDC assume-role, build, `terraform apply` reporting *No changes.
Your infrastructure matches the configuration* — meaning CI reads the same state a local apply
does, and the local and CI paths have converged.

**Verification** — `ruff format --check` and `ruff check` clean, `pytest` green, `terraform
validate` passing for the root module and the Lambda module, `lambda.zip` building at 571 KB.

**Not yet true**: no handler exists, so no route exists and no Lambda is deployed. The three
secrets are empty. Nothing in the system yet knows what an invoice is.

**Next**: Phase 3, the golden path — 15 tasks, and the MVP.

---
---

# APPENDED — Phase 3, the golden path

*Everything above documents Phases 1 and 2 and is unchanged. This section covers tasks
T032–T046: the golden path, from a verified caller to a payment sitting under human review.*

**Scope**: T032, T033, T036–T046. Phase 3 is the MVP — the journey the Loom shows.

---

## What Phase 3 delivered

A caller can ring a real phone number, be verified against real records, dispute a real
invoice, have their payment matched server-side, and leave with it under review, a ticket
raised, and an audit event written. Four tool endpoints are live behind API Gateway.

The remaining Phase 3 tasks are the two test tasks, T034 and T035.

---

## The tasks

### T032 / T040 — Payment matching

**Tests first, then the rule.** 28 cases written before any implementation, so the rule was
fixed by the requirement rather than by whatever the code happened to do.

Writing them produced three corrections to the spec, all from the user reading the tests:

**A 3-day backward date tolerance.** The payer reads the date their transfer *left*; the
record holds the date it *arrived*. Under exact matching a Friday transfer posting on Monday
tells an honest caller their payment does not exist. Backward only — a payment cannot post
before it was sent, so a later date is a genuine mismatch, not a banking artefact.

**Two invoice identifiers instead of one.** This is the most consequential design change in
the phase. `invoice_number` (`INV-2026-0412`) is sequential, spoken aloud, and never
fuzzy-matched. `payment_reference` (a random 7-digit key) is what customers quote on
transfers, and is the only field matched fuzzily.

The reason: consecutive invoice numbers differ by **one character**. Fuzzy-matching them
would classify a payment correctly earmarked for `INV-2026-0413` as a typo of `INV-2026-0412`
and propose allocating it to the wrong invoice — with the amount and date appearing to agree.
Silent, plausible, wrong. A random key over ten million values almost never has a real
neighbour, so a near miss that resolves to nothing is safely a typo.

**Payer name and address stopped affecting the outcome.** A payment may legitimately come
from a parent company or a third party; refusing it would strand real money. Both became
informational flags, and the rule receives booleans rather than values so it cannot leak
either.

**Two orderings in the implementation are load-bearing**, not incidental:

- Reference classification checks "resolves to another invoice" *before* "looks like a typo
  of this one". Reversed, a mistyped key landing on a real invoice reads as a typo and the
  payment goes to the wrong invoice.
- Amount and date are checked *before* the reference is classified at all, so a caller who
  has not demonstrated knowledge of the payment learns nothing from a failed attempt — not
  even that a payment exists.

### T037 / T038 — Verification

The disclosure gate. 22 domain tests and 20 contract tests.

**Writing the handler tests found a real security hole.** Failed attempts were counted only
per customer. A caller supplying a wrong answer without ever naming an account resolves to no
customer, so no counter moved and they could guess indefinitely. FR-006 says attempts are
counted per customer *and per caller session*; only the first half had been built. The
session counter now lives on the conversation record.

Three other decisions:

- **A locked account is refused before answers are evaluated.** Evaluating first would let a
  locked-out caller keep reading `factors_confirmed` to learn which answers were right,
  making the lockout a speed bump rather than a lock.
- **An unknown customer is carried through with an empty record** rather than returned early,
  so it is indistinguishable from wrong answers. Otherwise the gate enumerates customers.
- **Invented field names are dropped, not rejected.** A language model will occasionally
  hallucinate a field; failing the call for it turns that into a refused customer.

**Phone numbers are compared on their last nine digits.** `+41 44 123 45 67`,
`044 123 45 67` and `0041441234567` are one Swiss number written three ways, and a voice
agent transcribes whichever the caller speaks. Comparing raw strings refuses correct callers
routinely.

### T036 — The customer base

Ten companies and thirty contacts mirroring the HubSpot development account, linked by id,
with 91 ledger entries reaching back to April 2024.

**This is where the data boundary became real rather than asserted.** HubSpot holds names,
emails and companies. DynamoDB holds dates of birth, phone numbers, and every franc. A
reviewer can open both systems and check FR-027 and FR-028 hold, instead of taking the
spec's word for it.

Dates are offsets from the seeding date rather than fixed, so the overdue invoice is always
overdue and the disputed payment always sits inside the matching tolerance. Fixtures do not
go stale between rehearsals.

**Seeding deletes rows the fixtures no longer describe.** Overwriting alone left ten orphans
from the previous fixture set — phantom invoices that would quietly change a balance.

### T039 — Account context

The first tool to read financial data, and the first place the gate protects anything.

**It exposed a bug with a nasty shape.** `set_verification` updated the conversation record
*conditionally on it already existing*, and nothing created it — `start()` is only called by
the initiation webhook, which is T076 and not built. Verification returned VERIFIED, the
write was discarded, and every subsequent tool refused the caller.

A discarded verification write **looks identical to the disclosure gate working correctly.**
Refusing callers is what a working gate does. It only became visible by comparing what
verification returned against what the table actually held.

The contract tests could not have caught it: they mock `set_verification`, so the real module
never ran. Mocking what you depend on means never testing it.

**Then deploying hit IAM.** The handler read the identity table, which its policy
deliberately excludes. Rather than granting a third handler access to identity data,
verification now records the few non-sensitive fields downstream tools need — company name,
language, status, HubSpot ids — onto the conversation. Principle IV stays true rather than
aspirational, and a test asserts this handler never touches that table.

### T041 — Payment matching endpoint

**The asymmetry is the security property**: the caller supplies values and learns only
whether they matched. Nothing flows the other way.

A `_ReferenceResolver` answers "does this string resolve to a real invoice?" through two
indexed queries rather than loading every invoice in the system. It excludes the invoice
under discussion by entry id, so that invoice's own reference cannot look like another's.

A second GSI was added on `invoice_number`. Without it, a payment quoting an invoice number
belonging to a different invoice would be unresolvable, and the safe answer would have to be
a blanket refusal — which strands legitimate payments.

**A failed match returns `{"status": "NO_MATCH"}` and nothing else.** A test asserts that a
customer with no unallocated payments and a customer with a wrong amount produce byte-identical
responses, so probing reveals not even whether a payment exists.

### T042 / T043 — Allocation

The golden path's one mutation, and the only place the agent changes a financial record.

**State is checked before authority**, so raising the authority threshold can never become a
way to allocate a payment that has not arrived. The auto-allocate branch is unreachable under
the shipped policy of zero, but it exists and is tested rather than being an undefined
consequence of changing a parameter.

**Write order matters and is documented in the code**: the ledger moves first because it is
the only authoritative record; ticket and CRM log follow. If HubSpot fails afterwards the
payment is still under review and a human still finds it. The reverse would promise a caller
a review that does not exist.

**Everything survives being called twice.** The update is conditional on the payment still
being `UNALLOCATED`, so two concurrent calls cannot both create a review; a repeat returns
the original ticket rather than an error, so the agent can say something true and calm.

### T044 / T045 / T046 — The agent

The prompt and tool definitions live in git and reach ElevenLabs through `make agent-sync`,
not through a dashboard where a change leaves no trace and no review.

**Rewriting `tools.json` against the real API corrected two guesses.** Platform-injected
fields use `dynamic_variable` rather than a templated description, so `conversation_id` is
supplied by ElevenLabs instead of depending on the model to pass it correctly — and such a
field may carry no description, since the model never sees it. The API key reaches tools as a
workspace secret *reference*, so it exists in Secrets Manager and in ElevenLabs' secret store
and in no file.

Several prompt instructions exist because their absence has a specific cost: speak before
every tool call, because a silent pause sounds like a dropped line; offer to wait while the
caller checks their banking app, because exact-date matching requires a lookup; never say
"close" or "almost" on a wrong factor, because that turns verification into an oracle; never
speculate aloud about why a rule fired, because saying anything fraud-adjacent to a customer
who may be entirely honest is indefensible.

**The prompt names no company and no scenario.** Behaviour comes from principles, so it
generalises to callers the fixtures never anticipated.

---

## Failures encountered

Six more, on top of the six in Phases 1 and 2.

### 7. A customer-id enumeration oracle, found by deploying

Supplying a *real* customer id alongside deliberate nonsense returned `factors_confirmed: 1`;
an invented id returned `0`. Both `FAILED`, but the difference lets an attacker enumerate
customer ids across a five-digit space.

The unit test compared unknown-against-wrong using *identical* supplied values, so it was
structurally blind to it — two different ids was the case it never tried. Calling the
deployed endpoint with two different ids found it in seconds.

**Fix**: a failed attempt now reports zero confirmed, no non-document flag, and a hint
derived from nothing. Byte-identical responses, verified live.

**The lesson**: this is the argument for deploying early. The same bug found a week later
would have been buried under three other unfamiliar failures.

### 8. Verification written to nowhere

Covered under T039 above. The defining property is that the failure mode was indistinguishable
from correct behaviour.

### 9. Audit events landing in the wrong log group

`audit.write` logged to stdout, and Lambda routes stdout to the function's own log group —
**90-day retention**. FR-038a requires audit events to be kept ten years. The event was
complete and correct, and would have expired in three months.

**Fix**: audit events are now written directly to `/voice-agent/audit` through the CloudWatch
Logs API, with one stream per function per execution environment.

**Why it was easy to miss**: the event *looked* right in the logs. Nothing failed. Only asking
"which log group is this in?" surfaced it.

### 10. A dynamic variable with no webhook to supply it

Every inbound call died after one second with `agent_configuration_error 1008`. Twilio
reported 31921 — a WebSocket close — because from its side ElevenLabs simply hung up.

The tool definition referenced `candidate_customer_id`, which the conversation initiation
webhook supplies, and that webhook is T076. **The config ran ahead of the implementation.**
An agent-level placeholder did not satisfy the check; the field had to be removed until
there is something to provide it.

**The lesson**: referencing platform state before the thing that produces it exists fails at
call time, not at sync time, and the error surfaces three systems away from its cause.

### 11. An ASR provider and an LLM that were not available

Two separate config errors found only by calling. `agent.json` named an ASR provider that had
been removed, and `gpt-4o` returned "exceeds your quota limit" mid-call because premium LLMs
are metered separately on ElevenLabs.

Also learned: **ElevenLabs ties the TTS model to the agent's base language.** An English-base
agent must use the English-only models, while multilingual requires `eleven_turbo_v2_5`. So
FR-033's four languages cannot be delivered with English as the base language — the current
English setting is explicitly a rehearsal configuration, and T080 reverts it.

### 12. Editing in the dashboard, silently reverted

A language change made in the ElevenLabs UI was overwritten by the next `make agent-sync`.
The repository is the source of truth by design, which means dashboard edits are lost rather
than merged. Worth stating plainly because it looks like the tool failing.

---

## Changes that came from reading the artifacts

Three requirements were added because the user read the generated test-scenario document and
found gaps the spec had not anticipated:

- **Authority to act** (FR-007a–c). Working at a customer company is not authority over its
  account. An employee who is not the recorded contact fails verification like anyone else,
  gets no financial information at all, and is told that an authorised contact can add them —
  **without being told who those contacts are**, since naming them tells an unverified caller
  who to impersonate next.
- **Overpayments** (FR-017a–c). A surplus is offset against the next invoice by default. A
  caller who wants it back is requesting a refund, evaluated under the same CHF 100 / CHF 500
  limits as any other outbound amount.
- **Ambiguity** (FR-010j). When more than one invoice could be the one a caller means, the
  agent establishes which before matching anything, and escalates if it cannot.

---

## State at the end of Phase 3

**Deployed**: four tool endpoints — `verify-identity`, `get-account-context`, `match-payment`,
`propose-allocation`. 39 → 55 Terraform-managed resources.

**Data**: 10 customers, 91 ledger entries, 30 CRM contacts across 10 companies.

**Tests**: 152 passing.

**Proven live, end to end**: verified caller → overdue invoice → server-side payment match →
payment moved to `UNDER_REVIEW` → HubSpot ticket carrying the reason it never allocated →
audit event in the ten-year log group. Called twice, it returns the original ticket rather
than creating a second review.

**Documentation**: `docs/test-scenarios.md` is generated from the fixtures by `make
scenarios`, because every figure in it is relative to the seeding date and a hand-written
copy would be wrong the next morning.

**Not yet true**: credits, escalation and transfer are unbuilt, so a call that needs a human
has nowhere to go. Multilingual is a rehearsal configuration rather than the real one. The
conversation initiation webhook does not exist, so greetings do not yet arrive in the
caller's stored language.

**Next**: T034 and T035 close Phase 3, then US2 hardens verification — lockout, risk signals,
and the guessing detection the user asked for.

---

## APPEND — 2026-09-05: four simplifications

A read of the whole codebase for redundancy, then four changes applied one at a time. Each was
verified against the unit suite, the deployed integration suite and all six story simulations
before the next began, and each went through its own pull request.

| PR | Change | Net |
|---|---|---|
| #13 | One normalisation table in `verification.py` | −30 |
| #14 | `common/guessing.py`, shared by both verification endpoints | −101 |
| #16 | `common/http.py`, one response shape for seven endpoints | −91 |
| #17 | Three functions nothing calls, deleted | −22 |

**−244 lines. Behaviour unchanged**: 715 unit, 60/60 integration, and the story simulations
holding where they held before.

**PR #15** was not a simplification. `test_the_whole_journey` started failing between #14 and
#16, and the cause was outside the diff: HubSpot's search index runs about 1.5 seconds behind
its own writes, measured with a probe ticket. The test now waits, and the window it exposes is
recorded in §12.56 rather than papered over.

**US5 still fails its callback check**, as it did before any of this. It has now failed,
passed and failed again across three runs of code that did not change in between — which is
the point §12.54 makes. The disclosure gate is a control and has never failed; "mention the
callback before transferring" is a tendency and fails about half the time. Moving that line
out of the prompt and into the `create_escalation` response is the fix, and it is the user's
call, because §12.35 reverted exactly that kind of unilateral change to caller-facing
behaviour.

**Next**: the Loom recording (T101), and the US5 decision.

# Multilingual Voice Billing Agent

An inbound voice agent for a fictional Swiss B2B company, handling invoice, payment, credit and
billing-dispute calls in German, French, Italian and English.

ElevenLabs owns the dialogue, language detection, tool selection and transfer. Every decision that
matters financially is made by a Python Lambda behind API Gateway, against DynamoDB and S3. HubSpot
holds only non-sensitive CRM records. The agent asks, explains and recommends; it never decides.

All data is synthetic.

## The trust boundary

The agent is a good conversationalist and an unreliable narrator. Everything it says is a
tendency; everything the backend enforces is a control. The line between them is the design.

**The agent cannot see anything financial until the backend says `VERIFIED`.** Not because the
prompt forbids it — because `require_verified()` reads a row written by `verify_identity` and
raises otherwise. The model's opinion of whether someone sounds genuine is never consulted.

**The agent cannot write the financial ledger.** All seven agent-facing IAM roles carry an
explicit `Deny` on the ledger table, so a future `Allow` added by someone who does not know this
rule cannot reopen the path. A credit or an allocation becomes a HubSpot ticket; a person accepts
it; a scheduled `apply_decisions` Lambda — with no API Gateway route and no way in from a call —
re-reads the ledger, re-runs the rules, and writes only what still passes. A person accepting a
ticket says they are content for it to happen, not that the arithmetic works.

**The agent cannot see what it must not say.** `match_payment` compares the caller's claimed
amount and date server-side and returns a verdict, never the stored values. There is no response
in which the expected answer appears, so no prompt rule is needed to stop it being read out.

What is left to the prompt is what a prompt is good at: tone, judgement, when to offer a credit
nobody asked for, and how to be decent to someone who is annoyed.

### Why not more of it in the prompt

Because it was tried. `docs/decisions.md` §12.33 records a rule that was present, bolded, and
quoted the exact phrase it forbade — and the agent said that phrase to a caller anyway. §12.50
records a structured procedure that read a fabricated version of itself aloud. Both are why the
controls that matter are IAM statements and conditional writes rather than sentences.

## Documentation

| Document | What it holds |
|---|---|
| [spec.md](specs/001-voice-billing-agent/spec.md) | What the system must do, and why |
| [plan.md](specs/001-voice-billing-agent/plan.md) | Architecture and the constitution check |
| [research.md](specs/001-voice-billing-agent/research.md) | The decisions and what was rejected |
| [data-model.md](specs/001-voice-billing-agent/data-model.md) | Tables, keys and state transitions |
| [contracts/](specs/001-voice-billing-agent/contracts/) | Tool and webhook contracts |
| [quickstart.md](specs/001-voice-billing-agent/quickstart.md) | Deploy, seed and validate |
| [tasks.md](specs/001-voice-billing-agent/tasks.md) | The build list |
| [constitution.md](.specify/memory/constitution.md) | The principles the design is held to |

## Setup

```bash
uv sync --all-groups
make lint
make test          # 706 offline, no AWS credentials needed
```

### The four test layers

| Layer | Proves | Needs |
|---|---|---|
| `tests/unit` | The rules, as pure functions | nothing |
| `tests/contract` | Each handler, with every adapter replaced | nothing |
| `tests/integration` | The pieces are wired to each other | a deployed stack |
| `tests/conversation` | The agent behaves, via `simulate-conversation` | ElevenLabs credits |

The first two run in about a second and are what `make test` runs. The split matters: a unit
test can prove the code does not attempt a ledger write, and only an integration test can prove
IAM would refuse if it did. Both of this project's worst afternoons were permissions the code
needed and Terraform did not have, and neither was visible offline.

```bash
AWS_PROFILE=voice-agent-admin uv run pytest tests/integration -v
```

## Running it

```bash
uv run python -m scripts.seed.seed                       # synthetic customers and ledger
uv run python -m scripts.agent.sync --agent-id agent_...  # tools and prompt to ElevenLabs
DEMO_TRANSFER_NUMBER=+41... uv run python -m scripts.agent.transfer --agent-id agent_...
uv run python -m scripts.metrics --days 30               # the nine rates in FR-043
```

`docs/test-scenarios.md` is generated from the fixtures and is the script for calling the agent
by phone: ten companies, what is true of each account, and what the agent should do.

## Bootstrap (once per account)

Terraform cannot create the S3 bucket it uses as its own backend, and GitHub Actions cannot assume a
role that does not exist yet. Both are solved by one local apply:

```bash
export AWS_PROFILE=<your-sso-profile>   # must have AdministratorAccess on the target account
aws sso login

cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
cp infra/terraform/backend.hcl.example   infra/terraform/backend.hcl
$EDITOR infra/terraform/terraform.tfvars   # set aws_account_id
$EDITOR infra/terraform/backend.hcl        # set the state bucket name

TF_STATE_BUCKET=<your-state-bucket> make bootstrap   # creates and versions it

cd infra/terraform
terraform init -backend-config=backend.hcl
terraform apply
```

Both `terraform.tfvars` and `backend.hcl` are untracked: they carry the account id, which
has no business in a repository that may be shared.

The apply prints `deploy_role_arn`. Set it as the `AWS_DEPLOY_ROLE_ARN` repository variable in
GitHub, and CI takes over from there — no long-lived AWS keys anywhere.

If the account already has a GitHub OIDC provider, set `create_oidc_provider = false` in
`terraform.tfvars`; an account may only have one.

## Deployment target

A dedicated AWS account, region `eu-central-1`. The account id lives in the untracked
`infra/terraform/terraform.tfvars`, and the provider's `allowed_account_ids` guard makes an apply
against any other account fail rather than quietly succeed.

## What is here

```
src/domain/      pure rules — no I/O, so Principle II is checkable rather than aspirational
src/adapters/    the only code that touches anything external
src/handlers/    thin Lambda entry points
agent/prompt/    four prompts, held to each other by tests/unit/test_prompts_agree.py
infra/terraform/ every AWS resource, including the Deny that the design rests on
docs/decisions.md  every decision, with what it cost
```

`src/domain` importing nothing from `src/adapters` is the whole reason the rules can be tested
without a network, and why fault injection needs no production flag: the adapter is replaced,
not a switch flipped.

## What is not done

`docs/decisions.md` is honest about the tradeoffs; this is honest about the gaps.

- **The conversation-test layer is expensive and the results are not stable.** Running it twice
  produced different outcomes, which is the same finding as §12.33 from another angle.
- **HubSpot is load-bearing.** A credit request needs the CRM to raise the ticket and to compute
  the ceiling. If HubSpot is down the agent refuses rather than guessing, which is right, but it
  is a real availability cost bought deliberately.
- **`check_factor` is an enumeration oracle.** It tells a caller which detail failed, which the
  final verification refuses to. That is what buys the spelling and date recovery, and the bar
  itself has not moved — knowing an address exists says nothing about who is holding the phone.
- **The applier is scheduled, not triggered.** A webhook would put the ledger-writing path behind
  an inbound internet request, which is the blast radius the design exists to shrink. The cost is
  up to a minute of lag.

# Multilingual Voice Billing Agent

An inbound voice agent for a fictional Swiss B2B company, handling invoice, payment, credit and
billing-dispute calls in German, French, Italian and English.

ElevenLabs owns the dialogue, language detection, tool selection and transfer. Every decision that
matters financially is made by a Python Lambda behind API Gateway, against DynamoDB and S3. HubSpot
holds only non-sensitive CRM records. The agent asks, explains and recommends; it never decides.

All data is synthetic.

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
make test
```

## Bootstrap (once per account)

Terraform cannot create the S3 bucket it uses as its own backend, and GitHub Actions cannot assume a
role that does not exist yet. Both are solved by one local apply:

```bash
export AWS_PROFILE=voice-agent-admin   # SSO profile for account 199013204701, AdministratorAccess
aws sso login
make bootstrap                                    # creates and versions the state bucket
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
$EDITOR infra/terraform/terraform.tfvars          # set aws_account_id

cd infra/terraform && terraform init && terraform apply
```

The apply prints `deploy_role_arn`. Set it as the `AWS_DEPLOY_ROLE_ARN` repository variable in
GitHub, and CI takes over from there — no long-lived AWS keys anywhere.

If the account already has a GitHub OIDC provider, set `create_oidc_provider = false` in
`terraform.tfvars`; an account may only have one.

## Deployment target

Account `199013204701`, region `eu-central-1`, dedicated to this demo. The provider's
`allowed_account_ids` guard makes an apply against any other account fail.

# The AWS account for this project. Overridable, but defaulted so a fresh clone and any
# shell hit the right account regardless of a global AWS_PROFILE set elsewhere.
AWS_PROFILE ?= voice-agent-admin  # override for another environment
export AWS_PROFILE

.PHONY: fmt lint test deploy seed bootstrap

fmt:
	uv run ruff format .
	terraform -chdir=infra/terraform fmt -recursive

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run python scripts/check_no_secrets.py
	terraform -chdir=infra/terraform fmt -check -recursive

test:
	uv run pytest tests/unit tests/contract

deploy:
	terraform -chdir=infra/terraform init -backend-config=backend.hcl
	terraform -chdir=infra/terraform apply

seed:
	uv run python -m scripts.seed.seed --env dev

# Pushes the prompt and tool definitions to ElevenLabs. The agent's behaviour lives in this
# repository, not in a dashboard where a change leaves no trace.
AGENT_ID ?= agent_3501m1k6hy8yech93pn3gfets2tx
agent-sync:
	uv run python -m scripts.agent.sync --agent-id $(AGENT_ID)

agent-diff:
	uv run python -m scripts.agent.sync --agent-id $(AGENT_ID) --dry-run

# The most recent call, with its tool calls. `make calls` lists recent ones.
transcript:
	uv run python -m scripts.agent.transcript --agent-id $(AGENT_ID)

calls:
	uv run python -m scripts.agent.transcript --agent-id $(AGENT_ID) --list

# One-time only. Terraform cannot create the bucket it uses as its own backend.
# The name is passed in rather than committed, because it embeds the account id:
#   TF_STATE_BUCKET=my-state-bucket make bootstrap
bootstrap:
	@test -n "$(TF_STATE_BUCKET)" || (echo "TF_STATE_BUCKET is required" && exit 1)
	aws s3api create-bucket --bucket $(TF_STATE_BUCKET) \
		--region eu-central-1 \
		--create-bucket-configuration LocationConstraint=eu-central-1
	aws s3api put-bucket-versioning --bucket $(TF_STATE_BUCKET) \
		--versioning-configuration Status=Enabled

# The AWS account for this project. Overridable, but defaulted so a fresh clone and any
# shell hit the right account regardless of a global AWS_PROFILE set elsewhere.
AWS_PROFILE ?= voice-agent-admin
export AWS_PROFILE

.PHONY: fmt lint test deploy seed bootstrap

fmt:
	uv run ruff format .
	terraform -chdir=infra/terraform fmt -recursive

lint:
	uv run ruff format --check .
	uv run ruff check .
	terraform -chdir=infra/terraform fmt -check -recursive

test:
	uv run pytest tests/unit tests/contract

deploy:
	terraform -chdir=infra/terraform apply

seed:
	uv run python scripts/seed/seed.py --env dev

# One-time only. Terraform cannot create the bucket it uses as its own backend.
bootstrap:
	aws s3api create-bucket --bucket elevenlabs-agent-tfstate-199013204701 \
		--region eu-central-1 \
		--create-bucket-configuration LocationConstraint=eu-central-1
	aws s3api put-bucket-versioning --bucket elevenlabs-agent-tfstate-199013204701 \
		--versioning-configuration Status=Enabled

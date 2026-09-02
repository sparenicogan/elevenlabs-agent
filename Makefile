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

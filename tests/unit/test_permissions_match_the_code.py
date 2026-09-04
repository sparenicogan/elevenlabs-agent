"""Every secret a handler reads must be granted to the role that runs it.

Twice in one afternoon a handler gained a dependency and its IAM policy did not: the applier
was denied dynamodb:UpdateItem on the ledger after it learned to apply allocations, and
check_factor was denied the attempt salt after it started counting guesses. Both failed only
on a live call, one of them as a 504 because the denial burned the timeout in retries.

Terraform is parsed as text rather than planned, so this runs offline in milliseconds and
fails in CI rather than on a phone call.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
HANDLERS = ROOT / "src" / "handlers"
TERRAFORM = ROOT / "infra" / "terraform"

# Secret name in code to the Terraform resource that grants it.
SECRET_RESOURCES = {
    "tools/api-key": "aws_secretsmanager_secret.tool_api_key.arn",
    "verification/attempt-salt": "aws_secretsmanager_secret.attempt_salt.arn",
    "hubspot/private-app-token": "aws_secretsmanager_secret.hubspot_token.arn",
    "elevenlabs/api-key": "aws_secretsmanager_secret.elevenlabs_api_key.arn",
    "elevenlabs/webhook-secret": "aws_secretsmanager_secret.elevenlabs_webhook.arn",
}


def _policy_for(handler: str) -> str:
    """
    The Terraform policy document for one handler, as text.

    handler: the module name, e.g. "check_factor".

    Returns: everything from its policy document to the module that uses it.
    """
    for path in TERRAFORM.glob("*.tf"):
        text = path.read_text()
        marker = f'data "aws_iam_policy_document" "{handler}"'
        if marker in text:
            start = text.index(marker)
            end = text.index(f'module "{handler}"', start)
            return text[start:end]
    raise AssertionError(f"no policy document for {handler}")


def _secrets_read_by(handler: str) -> set[str]:
    """Every secret name the handler and the common modules it imports read."""
    source = (HANDLERS / f"{handler}.py").read_text()
    names = set(re.findall(r'secrets\.get\(\s*"([^"]+)"', source))

    for module in re.findall(r"from src\.common import ([^\n]+)", source):
        for imported in (m.strip() for m in module.split(",")):
            path = ROOT / "src" / "common" / f"{imported}.py"
            if path.exists():
                names |= set(re.findall(r'secrets\.get\(\s*"([^"]+)"', path.read_text()))
    return names


def _handlers() -> list[str]:
    return sorted(p.stem for p in HANDLERS.glob("*.py") if p.stem != "__init__")


def test_every_secret_a_handler_reads_is_granted_to_its_role():
    missing = []
    for handler in _handlers():
        try:
            policy = _policy_for(handler)
        except AssertionError:
            continue  # No policy document of its own; nothing to check.
        for name in _secrets_read_by(handler):
            resource = SECRET_RESOURCES.get(name)
            assert resource, f"unknown secret {name!r} read by {handler}"
            if resource not in policy:
                missing.append(f"{handler} reads {name} but its role is not granted it")
    assert not missing, "\n".join(missing)


def test_the_mapping_covers_every_secret_the_code_reads():
    """So a new secret cannot pass this file by being unrecognised."""
    used = set()
    for path in (ROOT / "src").rglob("*.py"):
        used |= set(re.findall(r'secrets\.get\(\s*"([^"]+)"', path.read_text()))
    assert used <= set(SECRET_RESOURCES), f"unmapped: {used - set(SECRET_RESOURCES)}"

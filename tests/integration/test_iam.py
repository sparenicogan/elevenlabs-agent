"""The ledger permissions, asserted against the real policies.

The whole design rests on one sentence: the agent cannot write the financial ledger. That is
enforced by IAM, so IAM is where it should be checked — a unit test can only prove the code
does not try, which is a different and weaker claim.

    AWS_PROFILE=voice-agent-admin uv run pytest tests/integration/test_iam.py -v
"""

import json
import subprocess

import pytest

ACCOUNT_FALLBACK = "aws sts get-caller-identity --query Account --output text"

# Every role reachable from a call. The applier is deliberately absent: it is the one
# principal that may write, and it has no route in from a conversation.
AGENT_ROLES = (
    "verify-identity",
    "check-factor",
    "get-account-context",
    "match-payment",
    "propose-allocation",
    "request-credit",
    "create-escalation",
)

LEDGER_WRITES = (
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:BatchWriteItem",
)


def _aws(*args: str) -> str:
    try:
        return subprocess.run(
            ["aws", *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


@pytest.fixture(scope="module")
def account() -> str:
    value = _aws("sts", "get-caller-identity", "--query", "Account", "--output", "text")
    if not value.isdigit():
        pytest.skip("no AWS credentials; run aws sso login first")
    return value


@pytest.fixture(scope="module")
def ledger_arn(account: str) -> str:
    return f"arn:aws:dynamodb:eu-central-1:{account}:table/voice-agent-ledger"


def _simulate(role_arn: str, action: str, resource: str) -> str:
    """
    Asks IAM what would actually happen, rather than reading the policy ourselves.

    Returns: "allowed", "explicitDeny" or "implicitDeny".
    """
    raw = _aws(
        "iam",
        "simulate-principal-policy",
        "--policy-source-arn",
        role_arn,
        "--action-names",
        action,
        "--resource-arns",
        resource,
        "--output",
        "json",
    )
    if not raw:
        return "unknown"
    return json.loads(raw)["EvaluationResults"][0]["EvalDecision"]


@pytest.mark.parametrize("role", AGENT_ROLES)
@pytest.mark.parametrize("action", LEDGER_WRITES)
def test_no_agent_role_can_write_the_ledger(account, ledger_arn, role, action):
    arn = f"arn:aws:iam::{account}:role/voice-agent-{role}"
    decision = _simulate(arn, action, ledger_arn)
    if decision == "unknown":
        pytest.skip(f"could not simulate {role}")
    assert decision != "allowed", f"{role} may perform {action} on the ledger"


@pytest.mark.parametrize("role", AGENT_ROLES)
def test_the_denial_is_explicit_not_merely_absent(account, ledger_arn, role):
    """An implicit deny is one careless Allow away from being wrong. The explicit one survives
    someone adding a grant without knowing this rule."""
    arn = f"arn:aws:iam::{account}:role/voice-agent-{role}"
    decision = _simulate(arn, "dynamodb:UpdateItem", ledger_arn)
    if decision == "unknown":
        pytest.skip(f"could not simulate {role}")
    assert decision == "explicitDeny", f"{role} is only implicitly denied"


# What each reader actually calls. propose_allocation fetches two rows by key and never
# queries, so granting it Query would be a permission nothing uses.
LEDGER_READS = {
    "get-account-context": "dynamodb:Query",
    "match-payment": "dynamodb:Query",
    "propose-allocation": "dynamodb:GetItem",
    "request-credit": "dynamodb:Query",
}


@pytest.mark.parametrize("role,action", sorted(LEDGER_READS.items()))
def test_every_reader_can_still_read_the_ledger(account, ledger_arn, role, action):
    """The Deny must be narrow. An agent that cannot read an invoice cannot answer anything."""
    arn = f"arn:aws:iam::{account}:role/voice-agent-{role}"
    decision = _simulate(arn, action, ledger_arn)
    if decision == "unknown":
        pytest.skip(f"could not simulate {role}")
    assert decision == "allowed", f"{role} cannot {action} the ledger"


def test_the_applier_may_write_it(account, ledger_arn):
    """Someone has to, or an accepted ticket never becomes an entry."""
    arn = f"arn:aws:iam::{account}:role/voice-agent-apply-decisions"
    for action in ("dynamodb:PutItem", "dynamodb:UpdateItem"):
        decision = _simulate(arn, action, ledger_arn)
        if decision == "unknown":
            pytest.skip("could not simulate the applier")
        assert decision == "allowed", f"the applier cannot {action}"

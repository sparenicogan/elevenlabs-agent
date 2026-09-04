"""The path a credit actually takes to the ledger, against the deployed system.

A person accepts a ticket and a scheduled Lambda turns it into an entry. Every piece is
tested in isolation elsewhere; what this proves is that they are joined — the CRM property
names match what the code reads, the applier's role permits the write it attempts, and the
conditional key makes a second run a no-op.

Both halves of that were broken at once and neither showed up offline: the property was
aws_customer_id and the code read customer_id, and the role held PutItem where an allocation
needs UpdateItem.

    AWS_PROFILE=voice-agent-admin uv run pytest tests/integration/test_applier.py -v
"""

import json
import pathlib
import subprocess
import tempfile
import time
import uuid

import httpx
import pytest

CUSTOMER_ID = "446019693775"
CHARGE_ENTRY = "inv_93775_004"
TIMEOUT = 20.0


def _shell(*command: str) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


@pytest.fixture(scope="module")
def hubspot_token() -> str:
    token = _shell(
        "aws",
        "secretsmanager",
        "get-secret-value",
        "--secret-id",
        "voice-agent/hubspot/private-app-token",
        "--query",
        "SecretString",
        "--output",
        "text",
    )
    if not token:
        pytest.skip("no HubSpot token; run aws sso login first")
    return token


@pytest.fixture
def ticket(hubspot_token: str):
    """A credit request as request_credit would raise one, cleaned up afterwards."""
    headers = {"Authorization": f"Bearer {hubspot_token}"}
    created = httpx.post(
        "https://api.hubapi.com/crm/v3/objects/tickets",
        headers=headers,
        json={
            "properties": {
                "subject": f"integration probe {uuid.uuid4().hex[:8]}",
                "hs_pipeline_stage": "1",
                "credit_amount": 40.0,
                "related_entry_id": CHARGE_ENTRY,
                "aws_customer_id": CUSTOMER_ID,
            }
        },
        timeout=TIMEOUT,
    )
    if created.status_code >= 400:
        pytest.skip(f"could not create a ticket: {created.status_code}")
    ticket_id = created.json()["id"]

    yield ticket_id, headers

    httpx.delete(
        f"https://api.hubapi.com/crm/v3/objects/tickets/{ticket_id}",
        headers=headers,
        timeout=TIMEOUT,
    )
    _shell(
        "aws",
        "dynamodb",
        "delete-item",
        "--table-name",
        "voice-agent-ledger",
        "--key",
        json.dumps({"customer_id": {"S": CUSTOMER_ID}, "entry_id": {"S": f"cn_{ticket_id}"}}),
    )


def _ledger_entry(entry_id: str) -> dict:
    raw = _shell(
        "aws",
        "dynamodb",
        "get-item",
        "--table-name",
        "voice-agent-ledger",
        "--key",
        json.dumps({"customer_id": {"S": CUSTOMER_ID}, "entry_id": {"S": entry_id}}),
        "--output",
        "json",
    )
    return json.loads(raw).get("Item", {}) if raw else {}


def _run_applier() -> dict:
    """
    Invokes the applier directly rather than waiting for its schedule.

    Returns: the per-run counts.

    The response goes to a temporary file. Under capture_output, /dev/stdout is the capture
    pipe rather than the terminal, so the payload never arrives.
    """
    with tempfile.NamedTemporaryFile(suffix=".json") as out:
        _shell(
            "aws",
            "lambda",
            "invoke",
            "--function-name",
            "voice-agent-apply-decisions",
            "--payload",
            "{}",
            "--cli-binary-format",
            "raw-in-base64-out",
            out.name,
        )
        raw = pathlib.Path(out.name).read_text().strip()
    if not raw:
        pytest.skip("could not invoke the applier")
    return json.loads(raw)


def _apply_until(ticket_id: str, attempts: int = 12) -> dict:
    """
    Runs the applier until the entry appears, or gives up.

    ticket_id: the accepted ticket.
    attempts:  how many runs to try, five seconds apart.

    Returns: the ledger entry, or {} if it never arrived.

    HubSpot's search index lags its writes by a few seconds, so a ticket accepted a moment ago
    is not yet returned by a search for accepted tickets. That is a property of the CRM, not a
    fault in the applier: on the schedule it runs every minute and picks it up on the next
    pass. Here it has to be waited for.
    """
    for _ in range(attempts):
        _run_applier()
        entry = _ledger_entry(f"cn_{ticket_id}")
        if entry:
            return entry
        time.sleep(5)
    return {}


def _accept(ticket_id: str, headers: dict) -> None:
    httpx.patch(
        f"https://api.hubapi.com/crm/v3/objects/tickets/{ticket_id}",
        headers=headers,
        json={"properties": {"request_outcome": "Accepted", "hs_pipeline_stage": "4"}},
        timeout=TIMEOUT,
    )


class TestAnAcceptedCreditReachesTheLedger:
    def test_it_is_written_only_once_the_person_has_accepted(self, ticket):
        ticket_id, headers = ticket
        _run_applier()
        assert not _ledger_entry(f"cn_{ticket_id}"), "applied before anyone accepted it"

        _accept(ticket_id, headers)
        entry = _apply_until(ticket_id)
        assert entry, "an accepted credit never reached the ledger"
        assert entry["type"]["S"] == "CREDIT_NOTE"
        # Negative, because a credit reduces what is owed.
        assert entry["amount"]["N"].startswith("-")
        assert entry["allocated_to"]["L"][0]["S"] == CHARGE_ENTRY
        assert entry["decision_source"]["S"] == "HUMAN_ACCEPTED"

    def test_running_it_again_writes_nothing(self, ticket):
        """The ledger id comes from the ticket, so the conditional write refuses a second."""
        ticket_id, headers = ticket
        _accept(ticket_id, headers)
        first = _apply_until(ticket_id)
        assert first, "an accepted credit never reached the ledger"

        counts = _run_applier()
        assert counts["already_applied"] >= 1
        assert _ledger_entry(f"cn_{ticket_id}") == first

    def test_the_run_reports_what_it_did(self, ticket):
        """A run failing on every ticket must not look like a run with nothing to do."""
        counts = _run_applier()
        assert set(counts) == {"applied", "already_applied", "refused", "failed"}

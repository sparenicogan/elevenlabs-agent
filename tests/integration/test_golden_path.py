"""The golden path, run against the deployed system.

Everything else in this suite proves a piece in isolation with the world mocked out. This
proves the pieces are wired to each other: API Gateway routes to the right Lambda, the
Lambda's IAM policy actually permits what its code attempts, DynamoDB holds what the
fixtures claim, and the conversation state written by one endpoint is read by the next.

Those are precisely the failures unit tests cannot see, and every one of them has bitten
this project at least once — a handler reading a table its policy excluded, a verification
write silently discarded, audit events landing in the wrong log group.

Requires AWS credentials and a deployed stack:

    AWS_PROFILE=voice-agent-admin uv run pytest tests/integration -v

Skipped otherwise, so `make test` stays offline and fast.
"""

import json
import subprocess
import time
import uuid
from decimal import Decimal

import httpx
import pytest

CUSTOMER_ID = "445909044455"
INVOICE_ENTRY = "inv_00417_006"
PAYMENT_ENTRY = "pay_00417_disputed"

# What the caller says. These are the two values the backend compares, and the only two the
# agent is allowed to ask for.
CLAIMED_AMOUNT = 4200.00
CLAIMED_TRANSFER_DATE = "2026-07-27"

VERIFICATION_FACTORS = [
    {"field": "customer_id", "value": CUSTOMER_ID},
    {"field": "email", "value": "klaus.mueller@alpina-tech.ch"},
    # National form, as a caller would say it. The stored value is international.
    {"field": "phone", "value": "044 501 22 18"},
]

REQUEST_TIMEOUT = 20.0


def _shell(*command: str) -> str:
    """Runs a command and returns its output, or an empty string if it fails."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


@pytest.fixture(scope="module")
def endpoint() -> str:
    """The deployed API base URL, from Terraform state."""
    url = _shell("terraform", "-chdir=infra/terraform", "output", "-raw", "api_endpoint")
    if not url.startswith("https://"):
        pytest.skip("no deployed stack; run terraform apply first")
    return url.rstrip("/")


@pytest.fixture(scope="module")
def api_key() -> str:
    """The tool API key, read from Secrets Manager rather than any file."""
    key = _shell(
        "aws",
        "secretsmanager",
        "get-secret-value",
        "--secret-id",
        "voice-agent/tools/api-key",
        "--query",
        "SecretString",
        "--output",
        "text",
    )
    if not key:
        pytest.skip("no AWS credentials, or the tool api key is not set")
    return key


@pytest.fixture
def reset_payment():
    """
    Puts the disputed payment back to UNALLOCATED before the test runs.

    The golden path mutates it, so without this the second run of the day asserts against a
    payment that is already under review. Resetting rather than tolerating both outcomes
    keeps the assertions exact — a test that accepts two answers cannot tell you which one
    it got.
    """
    import boto3

    table = boto3.resource("dynamodb").Table("voice-agent-ledger")
    table.update_item(
        Key={"customer_id": CUSTOMER_ID, "entry_id": PAYMENT_ENTRY},
        UpdateExpression="SET #s = :unallocated REMOVE allocated_to, review_ticket_id",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":unallocated": "UNALLOCATED"},
    )
    yield table
    # Left as the test found it, so a rehearsal immediately afterwards starts clean.
    table.update_item(
        Key={"customer_id": CUSTOMER_ID, "entry_id": PAYMENT_ENTRY},
        UpdateExpression="SET #s = :unallocated REMOVE allocated_to, review_ticket_id",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":unallocated": "UNALLOCATED"},
    )


class Caller:
    """One conversation with the deployed tools, as the agent would drive it."""

    def __init__(self, endpoint: str, api_key: str):
        self._endpoint = endpoint
        self._headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        self.conversation_id = f"itest_{uuid.uuid4().hex[:12]}"

    def call(self, tool: str, **body) -> dict:
        response = httpx.post(
            f"{self._endpoint}/tools/{tool}",
            headers=self._headers,
            json={"conversation_id": self.conversation_id, **body},
            timeout=REQUEST_TIMEOUT,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def verify(self, factors=None) -> dict:
        return self.call("verify-identity", factors=factors or VERIFICATION_FACTORS)


@pytest.fixture
def caller(endpoint, api_key) -> Caller:
    return Caller(endpoint, api_key)


@pytest.mark.integration
class TestTheGateHoldsInProduction:
    def test_nothing_is_disclosed_before_verification(self, caller):
        for tool, body in (
            ("get-account-context", {}),
            ("match-payment", {"invoice_entry_id": INVOICE_ENTRY, "claimed_amount": 1.0}),
            (
                "propose-allocation",
                {"payment_entry_id": PAYMENT_ENTRY, "invoice_entry_id": INVOICE_ENTRY},
            ),
        ):
            result = caller.call(tool, **body)
            assert result["status"] == "SERVICE_UNAVAILABLE", tool
            assert result["error_category"] == "NOT_AUTHORIZED", tool

    def test_a_wrong_api_key_is_refused(self, endpoint):
        rejected = Caller(endpoint, "not-the-key")
        assert rejected.verify()["error_category"] == "NOT_AUTHORIZED"

    def test_two_factors_are_not_enough(self, caller):
        result = caller.verify(factors=VERIFICATION_FACTORS[:2])
        assert result["status"] == "PARTIALLY_VERIFIED"
        assert result["factors_confirmed"] == 2

    def test_a_wrong_answer_reveals_no_progress(self, caller):
        """The enumeration oracle found by calling the deployed endpoint: a real customer id
        with nonsense must look exactly like an invented one."""
        real = caller.verify(
            factors=[
                {"field": "customer_id", "value": CUSTOMER_ID},
                {"field": "email", "value": "nonsense@example.invalid"},
            ]
        )
        invented = Caller(caller._endpoint, caller._headers["x-api-key"]).verify(
            factors=[
                {"field": "customer_id", "value": "CUST-99999"},
                {"field": "email", "value": "nonsense@example.invalid"},
            ]
        )
        assert real["status"] == invented["status"] == "FAILED"
        assert real["factors_confirmed"] == invented["factors_confirmed"] == 0


@pytest.mark.integration
class TestTheGoldenPath:
    def test_the_whole_journey(self, caller, reset_payment):
        """One disputed invoice, from an unverified caller to a payment under human review.

        Deliberately one test rather than several: the steps depend on each other through
        state held in DynamoDB, and splitting them would either re-run the earlier steps or
        share mutable state between tests.
        """
        # 1. Verification, with the phone number spoken as a caller would say it.
        verified = caller.verify()
        assert verified["status"] == "VERIFIED"
        assert verified["factors_confirmed"] == 3
        assert verified["non_document_factor_satisfied"] is True
        # Nothing more to ask once the bar is met.
        assert verified["next_factor_hint"] is None

        # 2. Context. The invoice the caller is ringing about is overdue and unpaid.
        context = caller.call("get-account-context")
        assert context["status"] == "OK"
        assert context["customer"]["company_name"] == "Alpina Tech"
        disputed = next(i for i in context["open_invoices"] if i["entry_id"] == INVOICE_ENTRY)
        assert disputed["status"] == "OVERDUE"
        assert Decimal(str(disputed["amount"])) == Decimal("4200.00")

        # 3. A wrong amount tells the caller nothing at all.
        wrong = caller.call(
            "match-payment",
            invoice_entry_id=INVOICE_ENTRY,
            claimed_amount=9999.00,
            claimed_transfer_date=CLAIMED_TRANSFER_DATE,
        )
        assert wrong == {"status": "NO_MATCH"}

        # 4. The right amount and date match, and report why it never allocated.
        matched = caller.call(
            "match-payment",
            invoice_entry_id=INVOICE_ENTRY,
            claimed_amount=CLAIMED_AMOUNT,
            claimed_transfer_date=CLAIMED_TRANSFER_DATE,
        )
        assert matched["status"] == "MATCH"
        assert matched["payment_entry_id"] == PAYMENT_ENTRY
        assert matched["covers_invoice"] is True
        assert matched["requires_human_allocation"] is True
        assert matched["reference_link"] == "ABSENT"
        assert matched["address_discrepancy"] is True
        # The response carries flags, never the values behind them.
        assert "Zollikon" not in json.dumps(matched)

        # 5. The allocation is proposed, not made.
        proposed = caller.call(
            "propose-allocation",
            payment_entry_id=PAYMENT_ENTRY,
            invoice_entry_id=INVOICE_ENTRY,
        )
        assert proposed["status"] == "UNDER_REVIEW"
        assert proposed["previous_status"] == "UNALLOCATED"
        assert proposed["new_status"] == "UNDER_REVIEW"
        assert proposed["resolution_target_hours"] == 24
        ticket_id = proposed["ticket_id"]
        assert ticket_id, "a ticket must exist before a human is asked to act"

        # 6. The ledger actually moved. The response is not the record.
        stored = reset_payment.get_item(
            Key={"customer_id": CUSTOMER_ID, "entry_id": PAYMENT_ENTRY}
        )["Item"]
        assert stored["status"] == "UNDER_REVIEW"
        assert stored["allocated_to"] == [INVOICE_ENTRY]
        assert stored["decision_source"] == "AGENT_PROPOSED"
        assert stored["approval_status"] == "PENDING"

        # 7. Called again, it returns the same ticket rather than a second review.
        repeat = caller.call(
            "propose-allocation",
            payment_entry_id=PAYMENT_ENTRY,
            invoice_entry_id=INVOICE_ENTRY,
        )
        assert repeat["status"] == "ALREADY_UNDER_REVIEW"
        assert repeat["ticket_id"] == ticket_id

    def test_the_date_tolerance_works_against_the_real_record(self, caller):
        """A Friday transfer posting on Monday. The reason the tolerance exists is that
        without it an honest caller is told their payment does not exist."""
        caller.verify()
        three_days_earlier = "2026-07-24"
        result = caller.call(
            "match-payment",
            invoice_entry_id=INVOICE_ENTRY,
            claimed_amount=CLAIMED_AMOUNT,
            claimed_transfer_date=three_days_earlier,
        )
        assert result["status"] == "MATCH"

    def test_a_day_beyond_the_tolerance_does_not_match(self, caller):
        caller.verify()
        result = caller.call(
            "match-payment",
            invoice_entry_id=INVOICE_ENTRY,
            claimed_amount=CLAIMED_AMOUNT,
            claimed_transfer_date="2026-07-23",
        )
        assert result["status"] == "NO_MATCH"


@pytest.mark.integration
class TestTheAuditTrail:
    def test_a_proposal_writes_an_audit_event_to_the_ten_year_log_group(
        self, caller, reset_payment
    ):
        """The event landed in the Lambda's own 90-day group once, looking entirely correct.
        Which group it is in is the whole requirement (FR-038a)."""
        import boto3

        caller.verify()
        caller.call(
            "propose-allocation",
            payment_entry_id=PAYMENT_ENTRY,
            invoice_entry_id=INVOICE_ENTRY,
        )

        logs = boto3.client("logs")
        deadline = time.time() + 30
        events: list[dict] = []
        while time.time() < deadline and not events:
            events = logs.filter_log_events(
                logGroupName="/voice-agent/audit",
                startTime=int((time.time() - 300) * 1000),
                filterPattern=f'{{ $.conversation_id = "{caller.conversation_id}" }}',
            )["events"]
            if not events:
                time.sleep(2)

        assert events, "no audit event reached /voice-agent/audit"
        record = json.loads(events[-1]["message"])

        # Every field FR-041 requires. An event missing one cannot be reconstructed later.
        for field in (
            "action",
            "previous_state",
            "new_state",
            "authorizing_rule",
            "customer_id",
            "conversation_id",
            "agent_version",
            "risk_result",
            "human_approval_required",
            "timestamp",
        ):
            assert field in record, f"audit event missing {field}"

        assert record["action"] == "propose_allocation"
        assert record["previous_state"] == "UNALLOCATED"
        assert record["new_state"] == "UNDER_REVIEW"
        assert record["human_approval_required"] is True

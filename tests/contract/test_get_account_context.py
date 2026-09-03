"""The get_account_context endpoint (contracts/tools.md).

This is the first tool that reads financial data, so it is the first place the disclosure
gate protects anything. Most of these tests are about what it refuses and what it survives.
"""

import json
from decimal import Decimal

import pytest

from src.adapters.errors import ErrorCategory, ToolError

API_KEY = "test-key"

# What verification recorded on the conversation. This handler holds no permission on the
# identity table and reads nothing from it.
DISPLAY = {
    "company_name": "Alpina Tech",
    "preferred_language": "en",
    "account_status": "ACTIVE",
    "hubspot_contact_id": "859557757171",
    "hubspot_company_id": "445909044455",
}

LEDGER = [
    {
        "entry_id": "inv_00417_006",
        "type": "INVOICE",
        "invoice_number": "INV-2026-0013",
        "amount": Decimal("4200.00"),
        "currency": "CHF",
        "entry_date": "2026-06-20",
        "due_date": "2026-07-20",
        "status": "OVERDUE",
    },
    {
        "entry_id": "inv_00417_007",
        "type": "INVOICE",
        "invoice_number": "INV-2026-0028",
        "amount": Decimal("980.00"),
        "currency": "CHF",
        "entry_date": "2026-08-06",
        "due_date": "2026-09-05",
        "status": "OPEN",
    },
    {
        "entry_id": "pay_00417_005",
        "type": "PAYMENT",
        "amount": Decimal("-4310.00"),
        "status": "ALLOCATED",
    },
    {
        "entry_id": "inv_00417_005",
        "type": "INVOICE",
        "invoice_number": "INV-2026-0004",
        "amount": Decimal("4310.00"),
        "status": "PAID",
        "due_date": "2026-04-10",
    },
]


@pytest.fixture
def stubs(mocker):
    """Replaces every adapter, so a dependency can fail without a flag existing in
    production code (research D8)."""
    from src.handlers import get_account_context as module

    mocker.patch.object(module.secrets, "get", return_value=API_KEY)
    mocker.patch.object(
        module.policy_module, "load", return_value=mocker.Mock(summary_max_chars=2000)
    )
    return {
        "verified": mocker.patch.object(
            module.conversation_state,
            "verified_context",
            return_value=("CUST-00417", dict(DISPLAY)),
        ),
        "get": mocker.patch.object(module.dynamo, "get", return_value=None),
        "query": mocker.patch.object(module.dynamo, "query", return_value=list(LEDGER)),
        "contact": mocker.patch.object(
            module.hubspot, "get_contact", return_value={"firstname": "Klaus"}
        ),
        "tickets": mocker.patch.object(module.hubspot, "get_open_tickets", return_value=[]),
        "module": module,
    }


def call(stubs, api_key=API_KEY, conversation_id="conv_1"):
    response = stubs["module"].handler(
        {
            "headers": {"x-api-key": api_key},
            "body": json.dumps({"conversation_id": conversation_id}),
        }
    )
    return json.loads(response["body"])


class TestTheGate:
    def test_an_unverified_conversation_is_refused(self, stubs):
        stubs["verified"].side_effect = ToolError(
            ErrorCategory.NOT_AUTHORIZED, "conversation is not verified"
        )
        result = call(stubs)
        assert result["status"] == "SERVICE_UNAVAILABLE"
        assert result["error_category"] == "NOT_AUTHORIZED"

    def test_a_refused_call_reads_no_financial_data(self, stubs):
        """The refusal must come before the ledger is touched, or an outage in the gate
        would be the only thing standing between a caller and their data."""
        stubs["verified"].side_effect = ToolError(ErrorCategory.NOT_AUTHORIZED, "no")
        call(stubs)
        stubs["query"].assert_not_called()

    def test_a_wrong_api_key_is_refused_before_verification_is_even_checked(self, stubs):
        call(stubs, api_key="wrong")
        stubs["verified"].assert_not_called()

    def test_the_customer_comes_from_verification_not_from_the_caller(self, stubs):
        """The request carries only a conversation id. There is no field a caller could use
        to name someone else's account."""
        call(stubs)
        assert stubs["query"].call_args.args[0] == "ledger"
        stubs["verified"].assert_called_once_with("conv_1")

    def test_the_handler_never_touches_the_identity_table(self, stubs):
        """Principle IV holds because only two handlers can read identity data. Everything
        this one needs about the customer was recorded on the conversation at verification.
        The IAM policy grants it no access here, so a read would fail in production — this
        test catches it before deploy."""
        call(stubs)
        tables_read = [c.args[0] for c in stubs["get"].call_args_list]
        assert "customer-identity" not in tables_read


class TestInvoices:
    def test_only_unsettled_invoices_are_returned(self, stubs):
        result = call(stubs)
        numbers = [i["invoice_number"] for i in result["open_invoices"]]
        assert numbers == ["INV-2026-0013", "INV-2026-0028"]

    def test_payments_are_not_reported_as_invoices(self, stubs):
        result = call(stubs)
        assert all(i["entry_id"].startswith("inv_") for i in result["open_invoices"])

    def test_invoices_are_ordered_by_due_date(self, stubs):
        result = call(stubs)
        due = [i["due_date"] for i in result["open_invoices"]]
        assert due == sorted(due)

    def test_the_outstanding_total_is_derived_not_stored(self, stubs):
        result = call(stubs)
        assert result["total_outstanding"] == 5180.00

    def test_a_customer_owing_nothing_gets_an_empty_list_not_an_error(self, stubs):
        stubs["query"].return_value = []
        result = call(stubs)
        assert result["status"] == "OK"
        assert result["open_invoices"] == []
        assert result["total_outstanding"] == 0

    def test_a_ledger_outage_is_never_reported_as_owing_nothing(self, stubs):
        """The distinction Principle III turns on. An empty list means 'nothing owed'; a
        failure must not be able to produce one."""
        stubs["query"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "table down")
        result = call(stubs)
        assert result["status"] == "SERVICE_UNAVAILABLE"
        assert "open_invoices" not in result

    def test_the_list_is_capped_but_the_count_is_not(self, stubs):
        """A caller with forty open invoices is a collections problem, not a phone call. The
        agent should know there are more without trying to read them all out."""
        stubs["query"].return_value = [
            {
                "entry_id": f"inv_{n:03d}",
                "type": "INVOICE",
                "invoice_number": f"INV-2026-{n:04d}",
                "amount": Decimal("100.00"),
                "due_date": f"2026-01-{n:02d}",
                "status": "OPEN",
            }
            for n in range(1, 21)
        ]
        result = call(stubs)
        assert len(result["open_invoices"]) == 8
        assert result["open_invoice_count"] == 20
        assert result["total_outstanding"] == 2000.00


class TestCrmDegradation:
    def test_a_crm_outage_does_not_cost_the_caller_their_invoices(self, stubs):
        """HubSpot is not the authority for money. Losing it must degrade the answer, not
        end the call (FR-025)."""
        stubs["contact"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "hubspot down")
        result = call(stubs)
        assert result["status"] == "OK"
        assert result["crm"] is None
        assert len(result["open_invoices"]) == 2

    def test_a_customer_with_no_crm_record_is_not_an_error(self, stubs):
        stubs["verified"].return_value = (
            "CUST-00417",
            {k: v for k, v in DISPLAY.items() if "hubspot" not in k},
        )
        result = call(stubs)
        assert result["status"] == "OK"
        assert result["crm"] is None


class TestSummary:
    def test_no_previous_call_means_no_summary(self, stubs):
        result = call(stubs)
        assert result["summary_text"] is None

    def test_a_summary_is_truncated_to_the_configured_cap(self, stubs):
        stubs["get"].return_value = {"summary_text": "x" * 5000}
        result = call(stubs)
        assert len(result["summary_text"]) == 2000

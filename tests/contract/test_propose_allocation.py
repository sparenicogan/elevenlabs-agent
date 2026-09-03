"""The propose_allocation endpoint (contracts/tools.md).

The one mutation the agent performs. These cover ownership, the write order, and what
happens when it is called twice — which it will be, because voice calls drop and callers
repeat themselves.
"""

import json
from decimal import Decimal

import pytest

from src.adapters.errors import ErrorCategory, ToolError

API_KEY = "test-key"

DISPLAY = {
    "company_name": "Alpina Tech",
    "hubspot_contact_id": "859557757171",
    "hubspot_company_id": "445909044455",
}

INVOICE = {
    "customer_id": "CUST-00417",
    "entry_id": "inv_00417_006",
    "type": "INVOICE",
    "invoice_number": "INV-2026-0013",
    "amount": Decimal("4200.00"),
    "due_date": "2026-07-20",
    "status": "OVERDUE",
}

PAYMENT = {
    "customer_id": "CUST-00417",
    "entry_id": "pay_00417_disputed",
    "type": "PAYMENT",
    "amount": Decimal("-4200.00"),
    "entry_date": "2026-07-27",
    "status": "UNALLOCATED",
    "reference": None,
    "payer_address": {"city": "Zollikon"},
}


@pytest.fixture
def stubs(mocker):
    """Replaces every adapter. Dynamo.get is routed by entry id so both reads are realistic."""
    from src.handlers import propose_allocation as module

    mocker.patch.object(module.secrets, "get", return_value=API_KEY)
    mocker.patch.object(
        module.policy_module,
        "load",
        return_value=mocker.Mock(allocation_authority_max=Decimal("0"), resolution_target_hours=24),
    )

    entries = {"inv_00417_006": dict(INVOICE), "pay_00417_disputed": dict(PAYMENT)}

    return {
        "entries": entries,
        "verified": mocker.patch.object(
            module.conversation_state,
            "verified_context",
            return_value=("CUST-00417", dict(DISPLAY)),
        ),
        "get": mocker.patch.object(
            module.dynamo, "get", side_effect=lambda t, k: entries.get(k["entry_id"])
        ),
        "update": mocker.patch.object(
            module.dynamo, "update_if", return_value={"status": "UNDER_REVIEW"}
        ),
        "ticket": mocker.patch.object(module.hubspot, "create_ticket", return_value="TICKET-1"),
        "interaction": mocker.patch.object(module.hubspot, "log_interaction"),
        "audit": mocker.patch.object(module.audit, "write"),
        "module": module,
    }


def call(stubs, api_key=API_KEY, **extra):
    payload = {
        "conversation_id": "conv_1",
        "payment_entry_id": "pay_00417_disputed",
        "invoice_entry_id": "inv_00417_006",
        **extra,
    }
    response = stubs["module"].handler(
        {"headers": {"x-api-key": api_key}, "body": json.dumps(payload)}
    )
    return json.loads(response["body"])


class TestTheGoldenPath:
    def test_the_payment_moves_under_review_not_allocated(self, stubs):
        """The agent has no authority to allocate. Under review is the furthest it goes."""
        result = call(stubs)
        assert result["status"] == "UNDER_REVIEW"
        assert result["new_status"] == "UNDER_REVIEW"
        assert result["previous_status"] == "UNALLOCATED"

    def test_the_write_is_conditional_on_the_status_not_having_moved(self, stubs):
        """Two concurrent calls must not both create a review. Only one can observe
        UNALLOCATED."""
        call(stubs)
        kwargs = stubs["update"].call_args.kwargs
        assert kwargs["condition"] == "#s = :expected"
        assert kwargs["ExpressionAttributeValues"][":expected"] == "UNALLOCATED"

    def test_an_audit_event_records_both_states_and_the_rule(self, stubs):
        event = stubs["audit"].call_args.args[0] if stubs["audit"].called else None
        call(stubs)
        event = stubs["audit"].call_args.args[0]
        assert event.previous_state == "UNALLOCATED"
        assert event.new_state == "UNDER_REVIEW"
        assert event.authorizing_rule == "allocation_above_authority"
        assert event.human_approval_required is True

    def test_a_ticket_is_created_and_linked_to_the_contact_and_company(self, stubs):
        call(stubs)
        kwargs = stubs["ticket"].call_args.kwargs
        assert kwargs["contact_id"] == "859557757171"
        assert kwargs["company_id"] == "445909044455"

    def test_the_ticket_tells_the_reviewer_why_it_never_allocated(self, stubs):
        call(stubs)
        content = stubs["ticket"].call_args.args[0]["content"]
        assert "no reference" in content
        assert "payer address differs" in content

    def test_the_caller_is_given_a_resolution_target(self, stubs):
        assert call(stubs)["resolution_target_hours"] == 24


class TestOwnership:
    def test_a_payment_belonging_to_someone_else_is_refused(self, stubs):
        stubs["entries"].pop("pay_00417_disputed")
        assert call(stubs)["error_category"] == "NOT_AUTHORIZED"

    def test_an_invoice_belonging_to_someone_else_is_refused(self, stubs):
        stubs["entries"].pop("inv_00417_006")
        assert call(stubs)["error_category"] == "NOT_AUTHORIZED"

    def test_nothing_is_written_when_ownership_fails(self, stubs):
        stubs["entries"].pop("pay_00417_disputed")
        call(stubs)
        stubs["update"].assert_not_called()
        stubs["ticket"].assert_not_called()


class TestCalledTwice:
    def test_a_payment_already_under_review_returns_its_original_ticket(self, stubs):
        """A repeat is not an error. The agent should say something true and calm rather
        than creating a second review (FR-022)."""
        stubs["entries"]["pay_00417_disputed"] = {
            **PAYMENT,
            "status": "UNDER_REVIEW",
            "review_ticket_id": "TICKET-ORIGINAL",
        }
        result = call(stubs)
        assert result["status"] == "ALREADY_UNDER_REVIEW"
        assert result["ticket_id"] == "TICKET-ORIGINAL"

    def test_a_repeat_creates_no_second_ticket_and_no_second_audit_event(self, stubs):
        stubs["entries"]["pay_00417_disputed"] = {**PAYMENT, "status": "UNDER_REVIEW"}
        call(stubs)
        stubs["ticket"].assert_not_called()
        stubs["audit"].assert_not_called()

    def test_losing_the_race_is_reported_as_already_under_review(self, stubs):
        """Another call moved the payment between this one's read and write."""
        stubs["update"].return_value = None
        assert call(stubs)["status"] == "ALREADY_UNDER_REVIEW"

    def test_an_allocated_payment_is_already_resolved(self, stubs):
        stubs["entries"]["pay_00417_disputed"] = {**PAYMENT, "status": "ALLOCATED"}
        assert call(stubs)["status"] == "ALREADY_ALLOCATED"

    def test_a_pending_payment_cannot_be_proposed(self, stubs):
        """Money that has not arrived must not be put under review."""
        stubs["entries"]["pay_00417_disputed"] = {**PAYMENT, "status": "PENDING"}
        assert call(stubs)["error_category"] == "VALIDATION"


class TestCrmDegradation:
    def test_a_crm_outage_does_not_stop_the_payment_going_under_review(self, stubs):
        """Losing the ticket is recoverable; failing to move the ledger would mean promising
        a caller a review that does not exist (FR-025)."""
        stubs["ticket"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "hubspot down")
        result = call(stubs)
        assert result["status"] == "UNDER_REVIEW"
        assert result["ticket_id"] is None
        stubs["update"].assert_called_once()

    def test_a_failed_interaction_log_does_not_undo_the_allocation(self, stubs):
        stubs["interaction"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "down")
        assert call(stubs)["status"] == "UNDER_REVIEW"

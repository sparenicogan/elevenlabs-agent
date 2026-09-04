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
    "customer_id": "445909044455",
    "entry_id": "inv_00417_006",
    "type": "INVOICE",
    "invoice_number": "INV-2026-0013",
    "amount": Decimal("4200.00"),
    "due_date": "2026-07-20",
    "status": "OVERDUE",
}

PAYMENT = {
    "customer_id": "445909044455",
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
            return_value=("445909044455", dict(DISPLAY)),
        ),
        "get": mocker.patch.object(
            module.dynamo, "get", side_effect=lambda t, k: entries.get(k["entry_id"])
        ),
        # Kept stubbed so that re-introducing a ledger write fails a test rather than
        # quietly shipping.
        "ledger_write": mocker.patch.object(module.dynamo, "update_if"),
        # The company's open reviews. Empty unless a test seeds one.
        "pending": mocker.patch.object(module.hubspot, "get_pending_requests", return_value=[]),
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

    def test_the_ledger_is_never_written(self, stubs):
        """The review is the ticket. The payment stays UNALLOCATED until a person accepts
        the proposal and the applier moves it."""
        call(stubs)
        stubs["ledger_write"].assert_not_called()

    def test_the_ticket_names_the_payment_and_what_it_settles(self, stubs):
        """The payment first, so the next caller's check reads it without parsing; the invoices
        after it, so the applier knows what to allocate the payment against."""
        call(stubs)
        properties = stubs["ticket"].call_args.args[0]
        assert properties["related_entry_id"] == "pay_00417_disputed,inv_00417_006"
        assert properties["aws_customer_id"] == "445909044455"

    def test_a_colleagues_open_review_is_matched_on_the_payment_not_the_whole_list(self, stubs):
        stubs["pending"].return_value = [
            {"id": "TICKET-COLLEAGUE", "related_entry_id": "pay_00417_disputed,inv_other"}
        ]
        assert call(stubs)["status"] == "ALREADY_UNDER_REVIEW"

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
        stubs["ledger_write"].assert_not_called()
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

    def test_a_colleague_who_already_asked_is_what_stops_a_second_review(self, stubs):
        """The case the user asked for: a different caller from the same company rings about
        the same payment. The ledger still says UNALLOCATED — the open ticket is the only
        thing that knows."""
        stubs["pending"].return_value = [
            {"id": "TICKET-COLLEAGUE", "related_entry_id": "pay_00417_disputed"}
        ]
        result = call(stubs)
        assert result["status"] == "ALREADY_UNDER_REVIEW"
        assert result["ticket_id"] == "TICKET-COLLEAGUE"
        stubs["ticket"].assert_not_called()

    def test_an_open_ticket_about_a_different_payment_does_not_block_this_one(self, stubs):
        stubs["pending"].return_value = [{"id": "TICKET-OTHER", "related_entry_id": "pay_other"}]
        assert call(stubs)["status"] == "UNDER_REVIEW"

    def test_an_allocated_payment_is_already_resolved(self, stubs):
        stubs["entries"]["pay_00417_disputed"] = {**PAYMENT, "status": "ALLOCATED"}
        assert call(stubs)["status"] == "ALREADY_ALLOCATED"

    def test_a_pending_payment_cannot_be_proposed(self, stubs):
        """Money that has not arrived must not be put under review."""
        stubs["entries"]["pay_00417_disputed"] = {**PAYMENT, "status": "PENDING"}
        assert call(stubs)["error_category"] == "VALIDATION"


class TestCrmDegradation:
    def test_a_crm_outage_is_not_reported_as_a_review(self, stubs):
        """The ticket is the review. If it was not created then nothing happened anywhere,
        and the one thing the agent must not do is say a colleague is looking into it."""
        stubs["ticket"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "hubspot down")
        result = call(stubs)
        assert result["status"] == "SERVICE_UNAVAILABLE"
        assert result.get("ticket_id") is None
        stubs["ledger_write"].assert_not_called()

    def test_an_unreadable_crm_does_not_raise_a_duplicate_review(self, stubs):
        """If the open reviews cannot be read, a colleague may already have asked. Guessing
        creates a second review of the same payment."""
        stubs["pending"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "down")
        assert call(stubs)["status"] == "SERVICE_UNAVAILABLE"
        stubs["ticket"].assert_not_called()

    def test_a_failed_interaction_log_does_not_undo_the_allocation(self, stubs):
        stubs["interaction"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "down")
        assert call(stubs)["status"] == "UNDER_REVIEW"

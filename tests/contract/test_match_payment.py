"""The match_payment endpoint (contracts/tools.md).

The rule itself is tested exhaustively in tests/unit/test_payment_match.py. These cover what
the handler adds: the gate, ownership, how ledger rows become candidates, and — most
importantly — what a failed attempt does not reveal.
"""

import json
from decimal import Decimal

import pytest

from src.adapters.errors import ErrorCategory, ToolError

API_KEY = "test-key"

INVOICE = {
    "customer_id": "445909044455",
    "entry_id": "inv_00417_006",
    "type": "INVOICE",
    "invoice_number": "INV-2026-0013",
    "payment_reference": "5028686",
    "amount": Decimal("4200.00"),
    "due_date": "2026-07-20",
    "status": "OVERDUE",
}

# The golden-path payment: right amount, no reference, and a payer address on record, which
# is only ever stored when it differs from the customer's.
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
    """Replaces every adapter so the handler is exercised alone."""
    from src.handlers import match_payment as module

    mocker.patch.object(module.secrets, "get", return_value=API_KEY)
    mocker.patch.object(
        module.policy_module,
        "load",
        return_value=mocker.Mock(payment_date_tolerance_days=3, reference_typo_max_distance=2),
    )
    return {
        "verified": mocker.patch.object(
            module.conversation_state, "verified_context", return_value=("445909044455", {})
        ),
        "get": mocker.patch.object(module.dynamo, "get", return_value=dict(INVOICE)),
        "query": mocker.patch.object(module.dynamo, "query", return_value=[dict(PAYMENT)]),
        "module": module,
    }


def call(stubs, amount="4200.00", transfer_date="2026-07-27", api_key=API_KEY, **extra):
    payload = {
        "conversation_id": "conv_1",
        "invoice_entry_id": "inv_00417_006",
        **({"claimed_amount": amount} if amount is not None else {}),
        **({"claimed_transfer_date": transfer_date} if transfer_date is not None else {}),
        **extra,
    }
    response = stubs["module"].handler(
        {"headers": {"x-api-key": api_key}, "body": json.dumps(payload)}
    )
    return json.loads(response["body"])


class TestTheGate:
    def test_an_unverified_conversation_is_refused(self, stubs):
        stubs["verified"].side_effect = ToolError(ErrorCategory.NOT_AUTHORIZED, "not verified")
        result = call(stubs)
        assert result["error_category"] == "NOT_AUTHORIZED"

    def test_a_refused_call_reads_no_ledger_data(self, stubs):
        stubs["verified"].side_effect = ToolError(ErrorCategory.NOT_AUTHORIZED, "not verified")
        call(stubs)
        stubs["query"].assert_not_called()

    def test_an_invoice_belonging_to_someone_else_is_refused(self, stubs):
        """The invoice id arrives from the conversation, and a model that has seen one
        customer's invoice could repeat it in another call (FR-007)."""
        stubs["get"].return_value = None
        result = call(stubs)
        assert result["error_category"] == "NOT_AUTHORIZED"

    def test_the_customer_is_never_taken_from_the_request(self, stubs):
        call(stubs, customer_id="CUST-99999")
        assert stubs["get"].call_args.args[1]["customer_id"] == "445909044455"

    def test_a_settled_invoice_cannot_be_claimed_against(self, stubs):
        """Matching against a paid invoice would let the same payment be proposed twice."""
        stubs["get"].return_value = {**INVOICE, "status": "PAID"}
        result = call(stubs)
        assert result["error_category"] == "VALIDATION"


class TestMatching:
    def test_the_golden_path_matches(self, stubs):
        result = call(stubs)
        assert result["status"] == "MATCH"
        assert result["payment_entry_id"] == "pay_00417_disputed"
        assert result["covers_invoice"] is True
        assert result["requires_human_allocation"] is True

    def test_an_address_on_the_payment_record_is_reported_as_a_discrepancy(self, stubs):
        """A payer address is only stored when it differs, so its presence is the mismatch."""
        assert call(stubs)["address_discrepancy"] is True

    def test_the_address_is_returned_so_the_caller_can_recognise_it(self, stubs):
        """Asking "did you move, or is that a typo?" without saying what the address is asks
        someone to confirm what they cannot see. By this point they have proved who they are
        and proved the payment is theirs by stating its amount and exact date."""
        assert call(stubs)["payer_address"] == "Zollikon"

    def test_a_full_address_is_spoken_as_one_line(self, stubs):
        stubs["query"].return_value = [
            {
                **PAYMENT,
                "payer_address": {
                    "street": "Alte Landstrasse 88",
                    "postcode": "8702",
                    "city": "Zollikon",
                },
            }
        ]
        assert call(stubs)["payer_address"] == "Alte Landstrasse 88, 8702 Zollikon"

    def test_no_address_is_returned_when_there_is_nothing_to_ask_about(self, stubs):
        """It is only ever the answer to a question the agent is about to ask."""
        stubs["query"].return_value = [{k: v for k, v in PAYMENT.items() if k != "payer_address"}]
        result = call(stubs)
        assert result["address_discrepancy"] is False
        assert result["payer_address"] is None

    def test_the_address_on_file_is_never_returned(self, stubs):
        """Only the one on the payment. The caller already knows their own address, and the
        question is whether the payment carries it."""
        assert "Industriestrasse" not in json.dumps(call(stubs))

    def test_a_transfer_date_inside_the_tolerance_matches(self, stubs):
        result = call(stubs, transfer_date="2026-07-24")
        assert result["status"] == "MATCH"

    def test_a_date_outside_the_tolerance_does_not(self, stubs):
        result = call(stubs, transfer_date="2026-07-23")
        assert result["status"] == "NO_MATCH"

    def test_payments_are_read_as_positive_amounts(self, stubs):
        """The ledger stores payments signed negative; the caller says '4200'. Comparing the
        stored sign against a caller's figure would never match."""
        result = call(stubs, amount="4200.00")
        assert result["status"] == "MATCH"


class TestWhatAFailureRevealsNothingAbout:
    def test_a_wrong_amount_returns_no_payment_details(self, stubs):
        result = call(stubs, amount="9999.00")
        assert result["status"] == "NO_MATCH"
        assert set(result) == {"status"}

    def test_a_customer_with_no_unallocated_payments_looks_identical(self, stubs):
        """Otherwise a caller could probe amounts and tell from the response shape whether
        any payment exists at all."""
        stubs["query"].return_value = []
        no_payments = call(stubs)
        stubs["query"].return_value = [dict(PAYMENT)]
        wrong_amount = call(stubs, amount="9999.00")
        assert no_payments == wrong_amount

    def test_missing_fields_are_named_but_nothing_else_is(self, stubs):
        result = call(stubs, amount=None, transfer_date=None)
        assert result["status"] == "INSUFFICIENT"
        assert set(result["missing_fields"]) == {"claimed_amount", "claimed_transfer_date"}
        assert "payment_entry_id" not in result


class TestDegradation:
    def test_a_ledger_outage_is_not_reported_as_no_match(self, stubs):
        """The distinction Principle III turns on: 'cannot check' is not 'did not happen'."""
        stubs["query"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "table down")
        result = call(stubs)
        assert result["status"] == "SERVICE_UNAVAILABLE"
        assert result["retryable"] is True

"""The applier: the only thing that writes the ledger.

What matters here is not that it applies accepted requests — that is the easy half. It is
that a person clicking Accepted is not sufficient. The rules run again, against the ledger as
it stands now, and refuse anything they would have refused on the call.
"""

from decimal import Decimal

import pytest

from src.adapters.errors import ErrorCategory, ToolError

CHARGE = {
    "customer_id": "446019693775",
    "entry_id": "inv_00982_004",
    "type": "INVOICE",
    "amount": Decimal("1420.00"),
    "status": "OPEN",
    "entry_date": "2026-07-25",
}

TICKET = {
    "id": "8801",
    "aws_customer_id": "446019693775",
    "related_entry_id": "inv_00982_004",
    "credit_amount": "40.00",
    "request_outcome": "Accepted",
}


@pytest.fixture
def stubs(mocker):
    from src.handlers import apply_decisions as module

    mocker.patch.object(
        module.policy_module,
        "load",
        return_value=mocker.Mock(
            credit_max_per_request=Decimal("100"),
            credit_max_rolling=Decimal("500"),
            credit_window_months=12,
        ),
    )
    return {
        "accepted": mocker.patch.object(
            module.hubspot, "get_accepted_requests", return_value=[dict(TICKET)]
        ),
        "query": mocker.patch.object(module.dynamo, "query", return_value=[dict(CHARGE)]),
        "put": mocker.patch.object(module.dynamo, "put_if_absent", return_value=True),
        "update": mocker.patch.object(
            module.dynamo, "update_if", return_value={"status": "ALLOCATED"}
        ),
        "note": mocker.patch.object(module.hubspot, "append_note"),
        "audit": mocker.patch.object(module.audit, "write"),
        "module": module,
    }


class TestApplying:
    def test_an_accepted_credit_reaches_the_ledger(self, stubs):
        assert stubs["module"].handler()["applied"] == 1

    def test_it_is_stored_negative_because_it_reduces_what_is_owed(self, stubs):
        stubs["module"].handler()
        assert stubs["put"].call_args.args[1]["amount"] == Decimal("-40.00")

    def test_it_is_attached_to_the_charge_the_caller_named(self, stubs):
        stubs["module"].handler()
        assert stubs["put"].call_args.args[1]["allocated_to"] == ["inv_00982_004"]

    def test_the_record_says_a_person_accepted_it(self, stubs):
        stubs["module"].handler()
        assert stubs["put"].call_args.args[1]["decision_source"] == "HUMAN_ACCEPTED"
        assert stubs["audit"].call_args.args[0].human_approval_required is True


class TestTheRulesRunAgain:
    """The reason this component exists. Accepted is a person's opinion; the ceilings are
    not, and they are checked against the ledger as it stands now rather than as it stood
    when the caller rang."""

    def test_a_credit_over_the_per_request_limit_is_refused_however_it_was_accepted(self, stubs):
        stubs["accepted"].return_value = [{**TICKET, "credit_amount": "250.00"}]
        assert stubs["module"].handler()["refused"] == 1
        stubs["put"].assert_not_called()

    def test_a_ceiling_breached_since_the_call_refuses_it(self, stubs):
        """Credits landed between the caller ringing and the ticket being accepted. The
        request was legitimate when it was made and is not any more."""
        stubs["query"].return_value = [
            dict(CHARGE),
            *[
                {
                    "type": "CREDIT_NOTE",
                    "amount": Decimal("-99.00"),
                    "entry_date": "2026-08-01",
                    "status": "APPROVED",
                }
                for _ in range(5)
            ],
        ]
        assert stubs["module"].handler()["refused"] == 1
        stubs["put"].assert_not_called()

    def test_a_charge_that_no_longer_exists_refuses(self, stubs):
        stubs["query"].return_value = []
        assert stubs["module"].handler()["refused"] == 1
        stubs["put"].assert_not_called()

    def test_a_refusal_is_written_where_a_person_will_see_it(self, stubs):
        stubs["accepted"].return_value = [{**TICKET, "credit_amount": "250.00"}]
        stubs["module"].handler()
        assert "Not applied" in stubs["note"].call_args.args[1]


PAYMENT = {
    "customer_id": "446019693775",
    "entry_id": "pay_1",
    "type": "PAYMENT",
    "amount": Decimal("-1420.00"),
    "status": "UNALLOCATED",
}

ALLOCATION = {
    "id": "9901",
    "aws_customer_id": "446019693775",
    "related_entry_id": "pay_1,inv_00982_004",
    "request_outcome": "Accepted",
}


class TestAllocations:
    """The other half of the loop. Until this existed an accepted allocation ticket was counted
    as already applied and silently did nothing, so the golden path ended at a ticket nobody
    could action without opening the table."""

    @staticmethod
    def _write_for(stubs, entry_id: str):
        """One run now writes the payment and every invoice it settles, so a test has to say
        which row it means rather than reading whichever call happened to be last."""
        for call in stubs["update"].call_args_list:
            if call.args[1]["entry_id"] == entry_id:
                return call.kwargs["ExpressionAttributeValues"]
        raise AssertionError(f"nothing was written to {entry_id}")

    def test_an_accepted_allocation_moves_the_payment(self, stubs):
        stubs["accepted"].return_value = [dict(ALLOCATION)]
        stubs["query"].return_value = [dict(CHARGE), dict(PAYMENT)]
        assert stubs["module"].handler()["applied"] == 1
        values = self._write_for(stubs, "pay_1")
        assert values[":new"] == "ALLOCATED"
        assert values[":invoices"] == ["inv_00982_004"]

    def test_the_move_is_conditional_so_a_second_run_writes_nothing(self, stubs):
        stubs["accepted"].return_value = [dict(ALLOCATION)]
        stubs["query"].return_value = [dict(CHARGE), dict(PAYMENT)]
        stubs["module"].handler()
        assert self._write_for(stubs, "pay_1")[":expected"] == "UNALLOCATED"

    def test_the_invoice_it_covers_stops_being_overdue(self, stubs):
        """The payment read ALLOCATED while the invoice it paid still read OVERDUE, so the
        next caller was told the thing they rang about last week was still outstanding."""
        stubs["accepted"].return_value = [dict(ALLOCATION)]
        stubs["query"].return_value = [dict(CHARGE), dict(PAYMENT)]
        stubs["module"].handler()
        values = self._write_for(stubs, "inv_00982_004")
        assert values[":paid"] == "PAID"
        assert values[":payment"] == "pay_1"

    def test_settling_an_invoice_twice_writes_once(self, stubs):
        stubs["accepted"].return_value = [dict(ALLOCATION)]
        stubs["query"].return_value = [dict(CHARGE), dict(PAYMENT)]
        stubs["module"].handler()
        for call in stubs["update"].call_args_list:
            if call.args[1]["entry_id"] == "inv_00982_004":
                assert call.kwargs["condition"] == "#s <> :paid"
                return
        raise AssertionError("the invoice was never settled")

    def test_a_payment_already_allocated_is_not_moved_again(self, stubs):
        stubs["accepted"].return_value = [dict(ALLOCATION)]
        stubs["query"].return_value = [dict(CHARGE), dict(PAYMENT)]
        stubs["update"].return_value = None
        counts = stubs["module"].handler()
        assert counts["already_applied"] == 1
        stubs["audit"].assert_not_called()

    def test_a_payment_that_does_not_cover_the_invoice_is_refused(self, stubs):
        """A person accepting a ticket says they are content for it to happen, not that the
        numbers add up."""
        stubs["accepted"].return_value = [dict(ALLOCATION)]
        stubs["query"].return_value = [dict(CHARGE), {**PAYMENT, "amount": Decimal("-100.00")}]
        assert stubs["module"].handler()["refused"] == 1
        stubs["update"].assert_not_called()
        assert "does not cover" in stubs["note"].call_args.args[1]

    def test_an_invoice_no_longer_on_the_account_is_refused(self, stubs):
        stubs["accepted"].return_value = [dict(ALLOCATION)]
        stubs["query"].return_value = [dict(PAYMENT)]
        assert stubs["module"].handler()["refused"] == 1
        stubs["update"].assert_not_called()

    def test_a_ticket_naming_no_invoice_fails_rather_than_guessing(self, stubs):
        stubs["accepted"].return_value = [{**ALLOCATION, "related_entry_id": "pay_1"}]
        stubs["query"].return_value = [dict(CHARGE), dict(PAYMENT)]
        assert stubs["module"].handler()["failed"] == 1


class TestRunTwice:
    def test_the_ledger_id_comes_from_the_ticket_so_a_rerun_writes_nothing(self, stubs):
        stubs["module"].handler()
        assert stubs["put"].call_args.args[1]["entry_id"] == "cn_8801"

    def test_an_already_applied_credit_is_not_audited_again(self, stubs):
        stubs["put"].return_value = False
        assert stubs["module"].handler()["already_applied"] == 1
        stubs["audit"].assert_not_called()


class TestDecisionsThatAreNotApprovals:
    """Rejected, Canceled by customer, and closed-without-an-outcome never reach the applier at
    all: the CRM search asks for Accepted, so an outcome it does not understand cannot become a
    ledger write by accident. Asserted on the query rather than on a branch, because there is no
    branch — which is the point."""

    def test_only_accepted_tickets_are_ever_fetched(self):
        import inspect

        from src.adapters import hubspot

        source = inspect.getsource(hubspot.get_accepted_requests)
        assert '"propertyName": "request_outcome"' in source
        assert '"value": "Accepted"' in source
        assert "Rejected" not in source
        assert "Canceled" not in source


class TestOneBadTicket:
    def test_it_does_not_hold_up_the_others(self, stubs):
        """A malformed request must not stop an unrelated customer being credited."""
        # A credit request with no customer on it: nothing can be read for it, and it must
        # not prevent the well-formed one behind it from being applied.
        stubs["accepted"].return_value = [
            {"id": "bad", "credit_amount": "10.00", "related_entry_id": "inv_1"},
            dict(TICKET),
        ]
        counts = stubs["module"].handler()
        assert counts == {"applied": 1, "already_applied": 0, "refused": 0, "failed": 1}

    def test_a_ledger_failure_on_one_ticket_is_counted_not_raised(self, stubs):
        stubs["put"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "down")
        assert stubs["module"].handler()["failed"] == 1


class TestARunIsVisible:
    """The counts exist so a run failing on every ticket cannot look like a run with nothing to
    do. That is not hypothetical: the applier was denied dynamodb:UpdateItem on the ledger for
    an hour and every run logged "apply run complete" and nothing else, because the count fields
    were not on the logging allowlist."""

    def test_the_count_fields_can_actually_be_logged(self):
        from src.common.logging import ALLOWED_FIELDS

        assert {"applied", "already_applied", "refused", "failed"} <= ALLOWED_FIELDS

    def test_every_outcome_is_counted(self, stubs):
        counts = stubs["module"].handler()
        assert set(counts) == {"applied", "already_applied", "refused", "failed"}

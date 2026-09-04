"""The request_credit endpoint (contracts/tools.md).

The rules are tested exhaustively in tests/unit/test_credit.py. These cover what the handler
adds: that evaluation cannot be skipped, that a repeat cannot be credited twice, and that a
refusal still leads somewhere.
"""

import json
from decimal import Decimal

import pytest

from src.adapters.errors import ErrorCategory, ToolError
from src.domain.risk import RiskSignal, SignalType

API_KEY = "test-key"

DISPLAY = {
    "company_name": "Ticino Industries",
    "account_status": "ACTIVE",
    "hubspot_contact_id": "859585377479",
    "hubspot_company_id": "31household",
}

INVOICE = {
    "customer_id": "446019693775",
    "entry_id": "inv_00982_004",
    "type": "INVOICE",
    "invoice_number": "INV-2026-0020",
    "amount": Decimal("1420.00"),
    "status": "OPEN",
    "entry_date": "2026-07-25",
}


@pytest.fixture
def stubs(mocker):
    from src.handlers import request_credit as module

    mocker.patch.object(module.secrets, "get", return_value=API_KEY)
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
        "verified": mocker.patch.object(
            module.conversation_state,
            "verified_context",
            return_value=("446019693775", dict(DISPLAY)),
        ),
        "signals": mocker.patch.object(module.conversation_state, "risk_signals", return_value=[]),
        # Read for the history patterns. Empty unless a test seeds a shape.
        "history": mocker.patch.object(
            module.conversation_state, "recent_conversations", return_value=[]
        ),
        "record_signal": mocker.patch.object(module.conversation_state, "record_risk_signal"),
        "query": mocker.patch.object(module.dynamo, "query", return_value=[dict(INVOICE)]),
        # The company's undecided requests. Empty unless a test seeds one.
        "pending": mocker.patch.object(module.hubspot, "get_pending_requests", return_value=[]),
        "ticket": mocker.patch.object(module.hubspot, "create_ticket", return_value="8801"),
        "audit": mocker.patch.object(module.audit, "write"),
        "module": module,
    }


def call(stubs, amount=40.00, entry_id="inv_00982_004", api_key=API_KEY, **extra):
    payload = {
        "conversation_id": "conv_1",
        "entry_id": entry_id,
        "amount": amount,
        "reason": "delivery was late",
        **extra,
    }
    response = stubs["module"].handler(
        {"headers": {"x-api-key": api_key}, "body": json.dumps(payload)}
    )
    return json.loads(response["body"])


class TestRequesting:
    def test_a_small_credit_on_a_clean_account_is_permitted(self, stubs):
        result = call(stubs)
        assert result["status"] == "REQUESTED"
        assert result["rule_applied"] == "within_credit_limits"

    def test_nothing_is_granted_only_asked_for(self, stubs):
        """The agent has no word for a credit it has given, because it cannot give one."""
        assert call(stubs)["status"] != "GRANTED"

    def test_the_ledger_is_never_written(self, stubs):
        """The whole point. This handler reads the ledger and writes a ticket."""
        module = stubs["module"]
        assert not hasattr(module.dynamo, "_write_called")
        call(stubs)
        assert stubs["ticket"].called

    def test_the_ticket_carries_the_numbers_the_next_call_adds_up(self, stubs):
        """credit_amount and related_entry_id are properties, not prose: the next caller's
        ceiling is computed by summing them."""
        call(stubs)
        properties = stubs["ticket"].call_args.args[0]
        assert properties["credit_amount"] == 40.00
        assert properties["related_entry_id"] == "inv_00982_004"

    def test_the_callers_reason_is_recorded_in_their_own_words(self, stubs):
        call(stubs, reason="the pallet arrived damaged")
        assert "the pallet arrived damaged" in stubs["ticket"].call_args.args[0]["content"]

    def test_it_is_associated_with_the_company_not_just_the_caller(self, stubs):
        """A colleague ringing tomorrow has to be able to see it."""
        call(stubs)
        assert stubs["ticket"].call_args.kwargs["company_id"] == "31household"

    def test_the_ticket_id_comes_back_so_the_agent_can_name_it(self, stubs):
        assert call(stubs)["ticket_id"] == "8801"

    def test_the_audit_event_says_a_person_still_has_to_accept_it(self, stubs):
        call(stubs)
        event = stubs["audit"].call_args.args[0]
        assert event.action == "request_credit"
        assert event.new_state == "REQUESTED"
        assert event.human_approval_required is True
        assert event.authorizing_rule == "within_credit_limits"


class TestEvaluationCannotBeSkipped:
    def test_there_is_no_way_to_issue_without_deciding(self, stubs):
        """One operation, not two. Two tools would leave a window in which the model calls
        the second without the first."""
        module = stubs["module"]
        assert not hasattr(module, "issue_credit")
        assert not hasattr(module, "issue")

    def test_a_refused_request_raises_no_ticket(self, stubs):
        call(stubs, amount=101.00)
        stubs["ticket"].assert_not_called()
        stubs["audit"].assert_not_called()

    def test_an_unverified_conversation_is_refused_before_the_ledger_is_read(self, stubs):
        stubs["verified"].side_effect = ToolError(ErrorCategory.NOT_AUTHORIZED, "no")
        result = call(stubs)
        assert result["error_category"] == "NOT_AUTHORIZED"
        stubs["query"].assert_not_called()


class TestRefusals:
    def test_over_the_per_request_limit(self, stubs):
        result = call(stubs, amount=101.00)
        assert result["status"] == "DENIED_LIMIT"
        assert result["rule_applied"] == "credit_max_per_request"

    def test_a_charge_that_does_not_exist(self, stubs):
        result = call(stubs, entry_id="inv_nonexistent")
        assert result["status"] == "DENIED_INELIGIBLE"
        assert result["rule_applied"] == "no_such_entry"

    def test_another_customers_charge_is_simply_not_there(self, stubs):
        """Ownership is implicit: the query is scoped to the verified customer, so an entry
        belonging to anyone else cannot be found (FR-007)."""
        stubs["query"].return_value = []
        assert call(stubs)["rule_applied"] == "no_such_entry"

    def test_a_suspended_account(self, stubs):
        stubs["verified"].return_value = (
            "446019693775",
            {**DISPLAY, "account_status": "SUSPENDED"},
        )
        assert call(stubs)["rule_applied"] == "account_status"

    def test_a_high_risk_caller_is_refused_a_request_that_would_otherwise_pass(self, stubs):
        stubs["signals"].return_value = [
            RiskSignal(SignalType.SUSPECTED_GUESSING, "3 distinct values", "conv_1")
        ]
        result = call(stubs)
        assert result["status"] == "DENIED_RISK"
        assert result["rule_applied"] == "risk_override"

    def test_every_refusal_tells_the_agent_to_hand_over(self, stubs):
        """A customer told only 'no' has been given nothing. Somebody with more authority
        may still say yes."""
        for amount in (101.00, 900.00):
            assert call(stubs, amount=amount)["should_escalate"] is True

    def test_an_incoherent_request_does_not_escalate(self, stubs):
        """A charge that does not exist is a conversation problem, not a policy one. Sending
        it to a person would waste their time."""
        assert call(stubs, entry_id="inv_nonexistent")["should_escalate"] is False


PENDING_40 = {"id": "8801", "credit_amount": "40.00", "related_entry_id": "inv_00982_004"}


class TestAskedTwice:
    """The open ticket is the idempotency record. There is no hash to collide: what stops a
    second ticket is that the first one is still sitting there unanswered."""

    def test_a_repeat_returns_the_open_ticket_rather_than_raising_another(self, stubs):
        stubs["pending"].return_value = [dict(PENDING_40)]
        result = call(stubs)
        assert result["ticket_id"] == "8801"
        stubs["ticket"].assert_not_called()

    def test_a_colleague_asking_from_another_call_gets_the_same_answer(self, stubs):
        """Scoped to the company, so it holds across conversations — which a per-call
        idempotency key never could."""
        stubs["pending"].return_value = [dict(PENDING_40)]
        assert call(stubs, conversation_id="conv_2")["ticket_id"] == "8801"
        stubs["ticket"].assert_not_called()

    def test_a_different_amount_is_a_new_request(self, stubs):
        stubs["pending"].return_value = [dict(PENDING_40)]
        call(stubs, amount=50.00)
        stubs["ticket"].assert_called_once()

    def test_a_pending_request_spends_the_entry_ceiling(self, stubs):
        """A credit can never exceed the charge it attaches to. An open request for CHF 40
        against a CHF 60 charge leaves CHF 20, even though nothing has reached the ledger —
        which is the case the old ledger-only total got wrong."""
        stubs["query"].return_value = [{**INVOICE, "amount": Decimal("60.00")}]
        stubs["pending"].return_value = [dict(PENDING_40)]
        result = call(stubs, amount=30.00)
        assert result["status"] == "DENIED_EXCEEDS_ENTRY"
        stubs["ticket"].assert_not_called()

    def test_pending_requests_count_towards_the_rolling_ceiling(self, stubs):
        """Five open requests of CHF 99 is CHF 495 of the CHF 500 the year allows, none of it
        yet in the ledger."""
        stubs["pending"].return_value = [
            {"id": str(n), "credit_amount": "99.00", "related_entry_id": f"other_{n}"}
            for n in range(5)
        ]
        result = call(stubs, amount=40.00)
        assert result["status"] == "DENIED_LIMIT"
        assert result["rule_applied"] == "credit_max_rolling"


class TestDegradation:
    def test_a_crm_outage_refuses_rather_than_granting_blind(self, stubs):
        """The ceiling lives in the CRM now. A read that failed is not a customer who has
        used no credit, and treating it as one gives the same headroom away twice."""
        stubs["pending"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "down")
        result = call(stubs)
        assert result["status"] == "SERVICE_UNAVAILABLE"
        stubs["ticket"].assert_not_called()

    def test_a_company_with_no_crm_record_is_refused(self, stubs):
        """Without a company there is no way to total what is already outstanding."""
        stubs["verified"].return_value = (
            "446019693775",
            {k: v for k, v in DISPLAY.items() if k != "hubspot_company_id"},
        )
        assert call(stubs)["status"] == "SERVICE_UNAVAILABLE"
        stubs["ticket"].assert_not_called()

    def test_a_ledger_outage_is_not_reported_as_a_refusal(self, stubs):
        """'Cannot check' is not 'not allowed'. Conflating them would refuse legitimate
        customers during an outage and record it as a policy decision."""
        stubs["query"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "down")
        result = call(stubs)
        assert result["status"] == "SERVICE_UNAVAILABLE"
        assert result["retryable"] is True


class TestThresholdSplitting:
    """US4. Five credits, none of them individually over the limit, adding to more than the
    year allows. The refusal comes from the ceiling; the signal explains the shape to whoever
    picks it up."""

    def _near_limit_credits(self, count: int) -> list[dict]:
        from datetime import date, timedelta

        today = date.today()
        return [
            {
                "type": "CREDIT_NOTE",
                "amount": Decimal("-90.00"),
                "entry_date": (today - timedelta(days=30 * (n + 1))).isoformat(),
                "status": "APPROVED",
            }
            for n in range(count)
        ]

    def test_a_history_of_near_limit_credits_raises_a_signal(self, stubs):
        stubs["query"].return_value = [dict(INVOICE), *self._near_limit_credits(5)]
        call(stubs, amount=80.00)
        raised = [c.args[0].signal_type for c in stubs["record_signal"].call_args_list]
        assert "SUSPECTED_THRESHOLD_SPLITTING" in raised

    def test_the_pattern_refuses_a_request_the_ceilings_would_allow(self, stubs):
        """Three near-limit credits total CHF 270, well inside the CHF 500 window. The
        ceilings would grant this; the pattern is what stops it."""
        stubs["query"].return_value = [dict(INVOICE), *self._near_limit_credits(3)]
        result = call(stubs, amount=40.00)
        assert result["status"] == "DENIED_RISK"
        assert result["should_escalate"] is True

    def test_a_clean_history_raises_nothing(self, stubs):
        call(stubs)
        stubs["record_signal"].assert_not_called()

    def test_two_near_limit_credits_are_not_yet_a_pattern(self, stubs):
        stubs["query"].return_value = [dict(INVOICE), *self._near_limit_credits(2)]
        assert call(stubs)["status"] == "REQUESTED"

    def test_the_signal_is_recorded_before_the_decision_is_taken(self, stubs):
        """Order matters: a signal detected after the decision could not have influenced it."""
        stubs["query"].return_value = [dict(INVOICE), *self._near_limit_credits(5)]
        call(stubs, amount=80.00)
        assert stubs["record_signal"].called
        assert stubs["ticket"].called is False

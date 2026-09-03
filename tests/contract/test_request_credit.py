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
}

INVOICE = {
    "customer_id": "CUST-00982",
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
            return_value=("CUST-00982", dict(DISPLAY)),
        ),
        "signals": mocker.patch.object(module.conversation_state, "risk_signals", return_value=[]),
        # Read for the history patterns. Empty unless a test seeds a shape.
        "history": mocker.patch.object(
            module.conversation_state, "recent_conversations", return_value=[]
        ),
        "record_signal": mocker.patch.object(module.conversation_state, "record_risk_signal"),
        "query": mocker.patch.object(module.dynamo, "query", return_value=[dict(INVOICE)]),
        "put": mocker.patch.object(module.dynamo, "put_if_absent", return_value=True),
        "interaction": mocker.patch.object(module.hubspot, "log_interaction"),
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


class TestGranting:
    def test_a_small_credit_on_a_clean_account_is_granted(self, stubs):
        result = call(stubs)
        assert result["status"] == "GRANTED"
        assert result["rule_applied"] == "within_credit_limits"

    def test_the_credit_note_is_written_against_the_named_charge(self, stubs):
        call(stubs)
        item = stubs["put"].call_args.args[1]
        assert item["type"] == "CREDIT_NOTE"
        assert item["allocated_to"] == ["inv_00982_004"]

    def test_it_is_stored_negative_because_it_reduces_what_is_owed(self, stubs):
        call(stubs)
        assert stubs["put"].call_args.args[1]["amount"] == Decimal("-40.00")

    def test_the_callers_reason_is_recorded_in_their_own_words(self, stubs):
        call(stubs, reason="the pallet arrived damaged")
        assert stubs["put"].call_args.args[1]["reason"] == "the pallet arrived damaged"

    def test_the_audit_event_says_no_human_approved_this(self, stubs):
        """The only autonomous financial action in the system. The record says so explicitly
        rather than by omission."""
        call(stubs)
        event = stubs["audit"].call_args.args[0]
        assert event.action == "issue_credit"
        assert event.human_approval_required is False
        assert event.authorizing_rule == "within_credit_limits"

    def test_it_is_marked_as_the_agent_acting_alone(self, stubs):
        assert (
            stubs["put"].call_args.args[1]["decision_source"] == "AGENT_AUTONOMOUS"
            if call(stubs)
            else True
        )


class TestEvaluationCannotBeSkipped:
    def test_there_is_no_way_to_issue_without_deciding(self, stubs):
        """One operation, not two. Two tools would leave a window in which the model calls
        the second without the first."""
        module = stubs["module"]
        assert not hasattr(module, "issue_credit")
        assert not hasattr(module, "issue")

    def test_a_refused_request_writes_nothing(self, stubs):
        call(stubs, amount=101.00)
        stubs["put"].assert_not_called()
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
        stubs["verified"].return_value = ("CUST-00982", {**DISPLAY, "account_status": "SUSPENDED"})
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


class TestCalledTwice:
    def test_the_same_request_produces_the_same_credit_id(self, stubs):
        """Derived from the conversation, the charge and the amount, so a repeat writes the
        same row rather than a second one."""
        first = call(stubs)["credit_entry_id"]
        second = call(stubs)["credit_entry_id"]
        assert first == second

    def test_a_repeat_does_not_write_a_second_credit(self, stubs):
        """put_if_absent returns False when the row exists. The caller is not credited
        twice for saying the same thing twice (FR-022)."""
        stubs["put"].return_value = False
        result = call(stubs)
        assert result["status"] == "GRANTED"
        stubs["audit"].assert_not_called()

    def test_a_different_amount_is_a_different_request(self, stubs):
        assert (
            call(stubs, amount=40.00)["credit_entry_id"]
            != call(stubs, amount=50.00)["credit_entry_id"]
        )


class TestDegradation:
    def test_a_crm_outage_does_not_undo_a_granted_credit(self, stubs):
        stubs["interaction"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "down")
        result = call(stubs)
        assert result["status"] == "GRANTED"
        stubs["put"].assert_called_once()

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
        assert call(stubs)["status"] == "GRANTED"

    def test_the_signal_is_recorded_before_the_decision_is_taken(self, stubs):
        """Order matters: a signal detected after the decision could not have influenced it."""
        stubs["query"].return_value = [dict(INVOICE), *self._near_limit_credits(5)]
        call(stubs, amount=80.00)
        assert stubs["record_signal"].called
        assert stubs["put"].called is False

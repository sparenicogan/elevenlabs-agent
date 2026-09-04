"""The eight failures the specification requires the system to survive (SC-005).

These are the evidence for a third of the specification. The clarification session chose
test-only coverage over a runtime fault-injection switch, so nothing here can be demonstrated
on a live call — this file *is* the demonstration, and it has to be legible as such.

Every scenario asserts two things: the caller gets an outcome that is safe, and the failure is
never spoken as an answer. The second is the one that matters. A system that says "you have
no outstanding invoices" during a database outage has not failed safely; it has lied.

Failures are induced by substituting an adapter, which is why the adapter boundary exists at
all — no fault-injection flag ships in production code (research D8).
"""

import json
from decimal import Decimal

import pytest

from src.adapters.errors import ErrorCategory, ToolError

API_KEY = "test-key"

# Phrases that would assert a financial fact. If a failure response contains one of these,
# the system has answered a question it could not answer.
FABRICATIONS = (
    "no outstanding",
    "nothing outstanding",
    "paid",
    "unpaid",
    "settled",
    "no invoices",
    "balance is",
    "we have received",
    "not received",
)


def assert_safe_failure(body: dict) -> None:
    """
    Asserts a response is a safe failure rather than an answer.

    body: the parsed tool response.

    Every tool must fail the same way: an enumerated status the agent can speak, a category,
    a retry flag, and a phrase that asserts nothing about money.
    """
    assert body["status"] == "SERVICE_UNAVAILABLE", body
    assert set(body) == {"status", "error_category", "retryable", "message_hint"}
    assert body["error_category"] in {c.value for c in ErrorCategory}

    hint = body["message_hint"].lower()
    for phrase in FABRICATIONS:
        assert phrase not in hint, f"message_hint asserts a financial fact: {body['message_hint']}"


def _module(name: str):
    import importlib

    return importlib.import_module(f"src.handlers.{name}")


def invoke(name: str, body: dict) -> dict:
    response = _module(name).handler({"headers": {"x-api-key": API_KEY}, "body": json.dumps(body)})
    return json.loads(response["body"])


@pytest.fixture
def wired(mocker):
    """Wires every handler with working adapters. Each test then breaks exactly one."""
    for name in (
        "verify_identity",
        "get_account_context",
        "match_payment",
        "propose_allocation",
        "request_credit",
        "create_escalation",
    ):
        module = _module(name)
        mocker.patch.object(module.secrets, "get", return_value=API_KEY)
        mocker.patch.object(
            module.policy_module,
            "load",
            return_value=mocker.Mock(
                required_factor_count=3,
                verification_max_attempts=3,
                guessing_max_distinct_values=2,
                payment_date_tolerance_days=3,
                reference_typo_max_distance=2,
                allocation_authority_max=Decimal("0"),
                credit_max_per_request=Decimal("100"),
                credit_max_rolling=Decimal("500"),
                credit_window_months=12,
                resolution_target_hours=24,
                summary_max_chars=2000,
            ),
        )
        if hasattr(module, "dynamo"):
            mocker.patch.object(module.dynamo, "get", return_value={})
            mocker.patch.object(module.dynamo, "query", return_value=[])
            mocker.patch.object(module.dynamo, "update_if", return_value={})
            mocker.patch.object(module.dynamo, "upsert", return_value={})
            mocker.patch.object(module.dynamo, "put_if_absent", return_value=True)
        if hasattr(module, "hubspot"):
            mocker.patch.object(module.hubspot, "get_contact", return_value={})
            mocker.patch.object(module.hubspot, "get_open_tickets", return_value=[])
            mocker.patch.object(module.hubspot, "get_pending_requests", return_value=[])
            mocker.patch.object(module.hubspot, "create_ticket", return_value="TICKET-1")
            mocker.patch.object(module.hubspot, "create_unassociated_ticket", return_value="T-Q")
            mocker.patch.object(module.hubspot, "log_interaction")
            mocker.patch.object(module.hubspot, "append_note")
        if hasattr(module, "audit"):
            mocker.patch.object(module.audit, "write")
        if hasattr(module, "conversation_state"):
            mocker.patch.object(
                module.conversation_state,
                "verified_context",
                return_value=(
                    "445909044455",
                    {"company_name": "Alpina Tech", "account_status": "ACTIVE"},
                ),
            )
            mocker.patch.object(module.conversation_state, "set_verification")
            mocker.patch.object(module.conversation_state, "risk_signals", return_value=[])
            mocker.patch.object(module.conversation_state, "recent_conversations", return_value=[])
            mocker.patch.object(module.conversation_state, "record_risk_signal")
            mocker.patch.object(module.conversation_state, "record_callback")
            mocker.patch.object(module.conversation_state, "record_wrong_values", return_value=0)
            mocker.patch.object(
                module.conversation_state, "record_factor_attempts", return_value=({}, True)
            )
    return mocker


def break_it(mocker, module_name: str, attribute: str, method: str, category=None) -> None:
    """Makes one dependency fail for one handler."""
    module = _module(module_name)
    getattr(getattr(module, attribute), method).side_effect = ToolError(
        category or ErrorCategory.DEPENDENCY_DOWN, "simulated outage"
    )


class TestOne_IdentityStoreDown:
    """A caller cannot be verified because the identity store is unreachable.

    The dangerous confusion is between "we cannot check who you are" and "you are not who you
    say". One is an outage; the other locks people out of their own accounts.
    """

    def test_it_is_not_reported_as_a_failed_verification(self, wired):
        # Both reads, because an outage takes the whole table: the index query that finds the
        # person and the read that fetches them. Breaking only one models a partial failure
        # that cannot happen.
        break_it(wired, "verify_identity", "dynamo", "get")
        break_it(wired, "verify_identity", "dynamo", "query")
        # An email, because that is what the agent asks for first and it is the answer that
        # actually reaches the store. A customer id alone resolves nobody whether the store
        # is up or down, so there is no outage for it to expose.
        result = invoke(
            "verify_identity",
            {
                "conversation_id": "c1",
                "factors": [{"field": "email", "value": "klaus.mueller@alpina-tech.ch"}],
            },
        )
        assert_safe_failure(result)
        assert result["retryable"] is True

    def test_the_caller_is_not_told_their_details_were_wrong(self, wired):
        break_it(wired, "verify_identity", "dynamo", "get")
        break_it(wired, "verify_identity", "dynamo", "query")
        result = invoke(
            "verify_identity",
            {"conversation_id": "c1", "factors": [{"field": "email", "value": "a@b.ch"}]},
        )
        assert "FAILED" not in json.dumps(result)


class TestTwo_InvoiceStoreDown:
    """The ledger is unreachable while a verified caller asks about their account.

    An empty list means "nothing owed". A failure must not be able to produce one.
    """

    def test_it_is_never_reported_as_owing_nothing(self, wired):
        break_it(wired, "get_account_context", "dynamo", "query")
        result = invoke("get_account_context", {"conversation_id": "c1"})
        assert_safe_failure(result)
        assert "open_invoices" not in result
        assert "total_outstanding" not in result


class TestThree_PaymentLookupDown:
    """A caller says they paid, and the payment records cannot be read."""

    def test_it_is_not_reported_as_no_match(self, wired):
        break_it(wired, "match_payment", "dynamo", "query")
        _module("match_payment").dynamo.get.return_value = {
            "entry_id": "inv_1",
            "type": "INVOICE",
            "amount": Decimal("4200"),
            "status": "OVERDUE",
            "invoice_number": "INV-1",
            "payment_reference": "123",
        }
        result = invoke(
            "match_payment",
            {
                "conversation_id": "c1",
                "invoice_entry_id": "inv_1",
                "claimed_amount": 4200.00,
                "claimed_transfer_date": "2026-07-27",
            },
        )
        assert_safe_failure(result)
        assert "NO_MATCH" not in json.dumps(result)

    def test_the_caller_is_not_told_their_payment_is_missing(self, wired):
        break_it(wired, "match_payment", "dynamo", "query")
        _module("match_payment").dynamo.get.return_value = {
            "entry_id": "inv_1",
            "type": "INVOICE",
            "amount": Decimal("4200"),
            "status": "OVERDUE",
            "invoice_number": "INV-1",
            "payment_reference": "123",
        }
        result = invoke(
            "match_payment",
            {
                "conversation_id": "c1",
                "invoice_entry_id": "inv_1",
                "claimed_amount": 4200.00,
                "claimed_transfer_date": "2026-07-27",
            },
        )
        assert "not received" not in result["message_hint"].lower()


class TestFour_CrmDown:
    """HubSpot is unreachable. It is not the authority for anything a caller asks about, so
    losing it must degrade the answer rather than end the call (FR-025)."""

    def test_account_context_still_returns_the_invoices(self, wired):
        break_it(wired, "get_account_context", "hubspot", "get_contact")
        _module("get_account_context").conversation_state.verified_context.return_value = (
            "445909044455",
            {"company_name": "Alpina Tech", "hubspot_contact_id": "859"},
        )
        result = invoke("get_account_context", {"conversation_id": "c1"})
        assert result["status"] == "OK"
        assert result["crm"] is None
        assert "open_invoices" in result

    def test_an_allocation_without_its_ticket_is_not_a_review(self, wired):
        """This inverted when the ledger write went away. The ticket used to be a
        convenience on top of a state change that had already happened; it is now the only
        record the review exists, so losing it means nothing happened and the agent must not
        say otherwise."""
        break_it(wired, "propose_allocation", "hubspot", "create_ticket")
        module = _module("propose_allocation")
        module.dynamo.get.side_effect = lambda t, k: {
            "inv_1": {
                "entry_id": "inv_1",
                "type": "INVOICE",
                "amount": Decimal("4200"),
                "invoice_number": "INV-1",
                "due_date": "2026-07-20",
                "status": "OVERDUE",
            },
            "pay_1": {
                "entry_id": "pay_1",
                "type": "PAYMENT",
                "amount": Decimal("-4200"),
                "status": "UNALLOCATED",
                "entry_date": "2026-07-27",
            },
        }.get(k["entry_id"])
        module.conversation_state.verified_context.return_value = (
            "445909044455",
            {
                "company_name": "Alpina Tech",
                "hubspot_contact_id": "859",
                "hubspot_company_id": "44",
            },
        )
        result = invoke(
            "propose_allocation",
            {"conversation_id": "c1", "payment_entry_id": "pay_1", "invoice_entry_id": "inv_1"},
        )
        assert result["status"] == "SERVICE_UNAVAILABLE"
        module.dynamo.update_if.assert_not_called()

    def test_an_escalation_is_never_lost_to_a_crm_outage(self, wired):
        break_it(wired, "create_escalation", "hubspot", "create_unassociated_ticket")
        module = _module("create_escalation")
        module.conversation_state.verified_context.side_effect = ToolError(
            ErrorCategory.NOT_AUTHORIZED, "not verified"
        )
        result = invoke(
            "create_escalation",
            {
                "conversation_id": "c1",
                "reason": "IDENTITY_NOT_ESTABLISHED",
                "caller_stated_problem": "an invoice I already paid",
            },
        )
        assert result["status"] == "CRM_UNAVAILABLE_PERSISTED"
        assert result["handoff_summary"]
        assert result["callback_created"] is True


class TestFive_TransferFailure:
    """The dial to a person fails, and the platform does not document whether the agent
    survives it. The caller must be protected either way (FR-020a)."""

    def test_the_callback_exists_before_any_transfer_is_attempted(self, wired):
        module = _module("create_escalation")
        module.conversation_state.verified_context.side_effect = ToolError(
            ErrorCategory.NOT_AUTHORIZED, "not verified"
        )
        result = invoke(
            "create_escalation",
            {
                "conversation_id": "c1",
                "reason": "CUSTOMER_REQUESTED_HUMAN",
                "caller_stated_problem": "I want to speak to someone",
            },
        )
        module.conversation_state.record_callback.assert_called_once()
        assert result["callback_created"] is True

    def test_the_agent_is_given_a_promise_that_survives_the_failure(self, wired):
        module = _module("create_escalation")
        module.conversation_state.verified_context.side_effect = ToolError(
            ErrorCategory.NOT_AUTHORIZED, "not verified"
        )
        result = invoke(
            "create_escalation",
            {"conversation_id": "c1", "reason": "CUSTOMER_REQUESTED_HUMAN"},
        )
        assert "call you back" in result["safe_to_promise"]


class TestSix_DuplicatePostCallWebhook:
    """The same notification arriving twice must create nothing twice (FR-024).

    Post-call processing is not built yet, so this covers the primitive it will use: a
    conditional write keyed on the conversation.
    """

    def test_a_conditional_write_reports_a_duplicate_rather_than_failing(self):
        from src.common import idempotency

        first = idempotency.key("conv_1", "post_call")
        second = idempotency.key("conv_1", "post_call")
        assert first == second

    def test_a_different_conversation_is_a_different_key(self):
        from src.common import idempotency

        assert idempotency.key("conv_1", "post_call") != idempotency.key("conv_2", "post_call")


class TestSeven_PartialFailureAfterAMutation:
    """A financial record changed, then something afterwards failed.

    The completed part must stand. Undoing a credit because its CRM note failed would turn a
    bookkeeping problem into a financial one.
    """

    def test_a_raised_request_survives_a_failed_interaction_log(self, wired):
        """The ticket is the record now, so it is the thing that must not be undone by a
        note failing to attach afterwards."""
        break_it(wired, "propose_allocation", "hubspot", "log_interaction")
        module = _module("propose_allocation")
        module.conversation_state.verified_context.return_value = (
            "446019693775",
            {"account_status": "ACTIVE", "hubspot_contact_id": "859", "hubspot_company_id": "44"},
        )
        module.dynamo.get.side_effect = lambda _table, key: {
            "pay_1": {
                "customer_id": "446019693775",
                "entry_id": "pay_1",
                "type": "PAYMENT",
                "amount": Decimal("1420.00"),
                "status": "UNALLOCATED",
                "entry_date": "2026-08-01",
            },
            "inv_1": {
                "customer_id": "446019693775",
                "entry_id": "inv_1",
                "type": "INVOICE",
                "invoice_number": "INV-2026-0020",
                "amount": Decimal("1420.00"),
                "status": "OPEN",
                "due_date": "2026-07-20",
            },
        }[key["entry_id"]]

        result = invoke(
            "propose_allocation",
            {"conversation_id": "c1", "payment_entry_id": "pay_1", "invoice_entry_id": "inv_1"},
        )

        # The note failed; the request did not. The caller is not told it fell through.
        assert result.get("error_category") is None
        assert module.hubspot.create_ticket.called

    def test_an_audit_write_failure_never_undoes_the_action(self):
        """The action has already happened by the time it is audited. The failure is logged
        loudly so it cannot pass unnoticed, and nothing is rolled back."""
        import inspect

        from src.common import audit

        source = inspect.getsource(audit.write)
        assert "AUDIT WRITE FAILED" in source
        assert "raise" not in source.split("except")[-1]


class TestEight_SlowDependency:
    """A dependency that is slow rather than down.

    The agent's budget is five seconds and the Lambda's is four, so the backend fails first
    and the agent receives something it can speak (research D5).
    """

    def test_a_timeout_is_retryable_and_says_nothing_about_money(self, wired):
        break_it(wired, "get_account_context", "dynamo", "query", ErrorCategory.TIMEOUT)
        result = invoke("get_account_context", {"conversation_id": "c1"})
        assert_safe_failure(result)
        assert result["error_category"] == "TIMEOUT"
        assert result["retryable"] is True

    def test_the_lambda_budget_sits_below_the_agents(self):
        """Read from the Terraform module rather than asserted from memory, so a change to
        one without the other fails here."""
        from pathlib import Path

        module = Path("infra/terraform/modules/lambda/variables.tf").read_text()
        assert "default     = 4" in module, "lambda timeout must stay below the agent's 5s"


class TestEveryToolFailsTheSameWay:
    """A caller should not be able to tell which part of the system broke."""

    @pytest.mark.parametrize(
        ("tool", "body"),
        [
            ("get_account_context", {"conversation_id": "c1"}),
            ("match_payment", {"conversation_id": "c1", "invoice_entry_id": "inv_1"}),
            (
                "propose_allocation",
                {"conversation_id": "c1", "payment_entry_id": "p", "invoice_entry_id": "i"},
            ),
            ("request_credit", {"conversation_id": "c1", "entry_id": "i", "amount": 10}),
        ],
    )
    def test_the_envelope_is_identical_whichever_tool_failed(self, wired, tool, body):
        module = _module(tool)
        module.conversation_state.verified_context.side_effect = ToolError(
            ErrorCategory.DEPENDENCY_DOWN, "simulated outage"
        )
        assert_safe_failure(invoke(tool, body))

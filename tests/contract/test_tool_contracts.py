"""Properties every tool endpoint must share, checked across all of them at once.

The per-handler files test what each tool does. This one tests what they must all do the
same way, because a property enforced in three places and forgotten in the fourth is not a
property — and the fourth is always the one that matters.

Two things it checks that nothing else can: that every tool speaks the same error language,
and that no tool leaks a stored value. The leak test works by seeding the adapters with
canary strings and asserting none of them survives into any response, so it catches a leak
introduced by a field added later that nobody thought to test.
"""

import json
from decimal import Decimal

import pytest

from src.adapters.errors import ErrorCategory, ToolError

API_KEY = "test-key"

# Values that exist only in the fake records below. If any appears in a response, something
# read from storage and handed it to the caller.
CANARY_EMAIL = "canary-email@example.invalid"
CANARY_PHONE = "+41 99 000 00 01"
CANARY_DOB = "1900-01-02"
CANARY_REFERENCE = "canary-reference-9999999"
CANARY_CITY = "Canaryville"
CANARY_PAYER = "Canary Holdings AG"

CANARIES = (CANARY_EMAIL, CANARY_PHONE, CANARY_DOB, CANARY_REFERENCE, CANARY_CITY, CANARY_PAYER)

IDENTITY = {
    "customer_id": "445909044455",
    "company_name": "Alpina Tech",
    "first_name": "Klaus",
    "last_name": "Mueller",
    "email": CANARY_EMAIL,
    "phone": CANARY_PHONE,
    "date_of_birth": CANARY_DOB,
    "preferred_language": "en",
    "account_status": "ACTIVE",
    "failed_verification_attempts": 0,
}

INVOICE = {
    "customer_id": "445909044455",
    "entry_id": "inv_1",
    "type": "INVOICE",
    "invoice_number": "INV-2026-0013",
    "payment_reference": CANARY_REFERENCE,
    "amount": Decimal("4200.00"),
    "due_date": "2026-07-20",
    "status": "OVERDUE",
}

PAYMENT = {
    "customer_id": "445909044455",
    "entry_id": "pay_1",
    "type": "PAYMENT",
    "amount": Decimal("-4200.00"),
    "entry_date": "2026-07-27",
    "status": "UNALLOCATED",
    "reference": CANARY_REFERENCE,
    "payer_name": CANARY_PAYER,
    "payer_address": {"city": CANARY_CITY},
}

# Every tool endpoint, with a request that would succeed if everything were permitted.
TOOLS = {
    "verify_identity": {
        "conversation_id": "conv_1",
        "factors": [{"field": "customer_id", "value": "445909044455"}],
    },
    "get_account_context": {"conversation_id": "conv_1"},
    "match_payment": {
        "conversation_id": "conv_1",
        "invoice_entry_id": "inv_1",
        "claimed_amount": 4200.00,
        "claimed_transfer_date": "2026-07-27",
    },
    "propose_allocation": {
        "conversation_id": "conv_1",
        "payment_entry_id": "pay_1",
        "invoice_entry_id": "inv_1",
    },
}

# Tools that must refuse an unverified conversation. verify_identity is the exception: it is
# how a conversation becomes verified in the first place.
GATED = ("get_account_context", "match_payment", "propose_allocation")


def _module(name: str):
    import importlib

    return importlib.import_module(f"src.handlers.{name}")


@pytest.fixture
def wired(mocker):
    """Wires every adapter for every handler, so one fixture serves all four."""
    entries = {"inv_1": dict(INVOICE), "pay_1": dict(PAYMENT)}

    for name in TOOLS:
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
                resolution_target_hours=24,
                summary_max_chars=2000,
            ),
        )
        if hasattr(module, "dynamo"):
            mocker.patch.object(
                module.dynamo,
                "get",
                side_effect=lambda t, k: entries.get(k.get("entry_id"), dict(IDENTITY)),
            )
            mocker.patch.object(module.dynamo, "query", return_value=[dict(PAYMENT)])
            mocker.patch.object(module.dynamo, "update_if", return_value={"status": "UNDER_REVIEW"})
            mocker.patch.object(module.dynamo, "upsert", return_value={})
        if hasattr(module, "hubspot"):
            mocker.patch.object(module.hubspot, "get_contact", return_value={})
            mocker.patch.object(module.hubspot, "get_open_tickets", return_value=[])
            mocker.patch.object(module.hubspot, "create_ticket", return_value="TICKET-1")
            mocker.patch.object(module.hubspot, "log_interaction")
        if hasattr(module, "audit"):
            mocker.patch.object(module.audit, "write")
        if hasattr(module, "conversation_state"):
            mocker.patch.object(
                module.conversation_state,
                "verified_context",
                return_value=("445909044455", {"company_name": "Alpina Tech"}),
            )
            mocker.patch.object(module.conversation_state, "set_verification")
            mocker.patch.object(module.conversation_state, "record_failed_attempt", return_value=1)
            mocker.patch.object(
                module.conversation_state, "record_factor_attempts", return_value=({}, True)
            )
            mocker.patch.object(module.conversation_state, "record_risk_signal")

    return mocker


def invoke(name: str, api_key: str = API_KEY, body: dict | None = None) -> dict:
    """Calls one tool as API Gateway would, and returns its parsed body."""
    response = _module(name).handler(
        {
            "headers": {"x-api-key": api_key},
            "body": json.dumps(body if body is not None else TOOLS[name]),
        }
    )
    return json.loads(response["body"])


@pytest.mark.parametrize("tool", list(TOOLS))
class TestEveryToolBehavesTheSame:
    """Properties that must hold identically across the whole tool surface."""

    def test_it_always_returns_a_status(self, wired, tool):
        assert "status" in invoke(tool)

    def test_it_always_returns_http_200(self, wired, tool):
        """A tool failure is a conversational outcome, not a transport error. Returning 4xx
        or 5xx would let the platform retry or surface a generic failure the agent cannot
        speak sensibly."""
        response = _module(tool).handler(
            {"headers": {"x-api-key": API_KEY}, "body": json.dumps(TOOLS[tool])}
        )
        assert response["statusCode"] == 200

    def test_it_refuses_a_wrong_api_key(self, wired, tool):
        result = invoke(tool, api_key="wrong")
        assert result["status"] == "SERVICE_UNAVAILABLE"
        assert result["error_category"] == "NOT_AUTHORIZED"

    def test_it_refuses_a_missing_conversation_id(self, wired, tool):
        result = invoke(tool, body={k: v for k, v in TOOLS[tool].items() if k != "conversation_id"})
        assert result["error_category"] == "VALIDATION"

    def test_its_error_envelope_has_every_field_the_contract_promises(self, wired, tool):
        result = invoke(tool, api_key="wrong")
        assert set(result) == {"status", "error_category", "retryable", "message_hint"}
        assert isinstance(result["retryable"], bool)

    def test_its_error_message_asserts_no_financial_fact(self, wired, tool):
        """message_hint is spoken to the caller. At the point it is returned the backend does
        not know a financial fact, so it must not imply one (FR-011)."""
        hint = invoke(tool, api_key="wrong")["message_hint"].lower()
        for forbidden in ("paid", "unpaid", "owe", "balance", "settled", "outstanding"):
            assert forbidden not in hint

    def test_it_never_returns_a_stored_value(self, wired, tool):
        """The canary test. Every stored record is seeded with values that exist nowhere
        else; none may survive into a response, whatever the outcome (FR-010, Principle IV)."""
        for body in (TOOLS[tool], {"conversation_id": "conv_1"}):
            serialised = json.dumps(invoke(tool, body=body))
            for canary in CANARIES:
                assert canary not in serialised, f"{tool} leaked {canary}"


@pytest.mark.parametrize("tool", GATED)
class TestTheGateAppliesEverywhere:
    """FR-001 is only true if it is true of every tool that reads financial data."""

    def test_an_unverified_conversation_is_refused(self, wired, tool):
        module = _module(tool)
        module.conversation_state.verified_context.side_effect = ToolError(
            ErrorCategory.NOT_AUTHORIZED, "not verified"
        )
        assert invoke(tool)["error_category"] == "NOT_AUTHORIZED"

    def test_a_refused_call_reads_no_financial_data(self, wired, tool):
        module = _module(tool)
        module.conversation_state.verified_context.side_effect = ToolError(
            ErrorCategory.NOT_AUTHORIZED, "not verified"
        )
        invoke(tool)
        module.dynamo.query.assert_not_called()

    def test_the_customer_is_never_taken_from_the_request(self, wired, tool):
        """No tool accepts a customer id. The only way to name an account is to verify
        against it."""
        polluted = {**TOOLS[tool], "customer_id": "CUST-99999"}
        assert "CUST-99999" not in json.dumps(invoke(tool, body=polluted))

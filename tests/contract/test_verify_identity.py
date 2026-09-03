"""The verify_identity endpoint (contracts/tools.md).

Covers what the handler does that the pure rule cannot: authentication, the lockout
counter, resolving whose record to check, and the shape of what goes back on the wire.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.domain.verification import Factor

API_KEY = "test-key"

RECORD = {
    "customer_id": "CUST-00417",
    "email": "buchhaltung@meier-bau.ch",
    "phone": "+41 44 123 45 67",
    "date_of_birth": "1974-03-12",
    "account_opening_year": "2019",
    "failed_verification_attempts": 0,
}


@pytest.fixture
def stubs(mocker):
    """Replaces every adapter, so these tests exercise the handler and nothing else.

    Substituting at the adapter boundary is what lets a dependency 'fail' without a
    fault-injection flag existing in production code (research D8)."""
    from src.handlers import verify_identity as module

    mocker.patch.object(module.secrets, "get", return_value=API_KEY)
    mocker.patch.object(
        module.policy_module,
        "load",
        return_value=mocker.Mock(
            required_factor_count=3, verification_max_attempts=3, guessing_max_distinct_values=2
        ),
    )
    return {
        "get": mocker.patch.object(module.dynamo, "get", return_value=dict(RECORD)),
        # Resolution by email or phone. Stubbed empty by default so tests that supply a
        # customer id take the direct path; overridden where the lookup is the subject.
        "query": mocker.patch.object(module.dynamo, "query", return_value=[]),
        "update": mocker.patch.object(module.dynamo, "update_if", return_value={}),
        "set_verification": mocker.patch.object(module.conversation_state, "set_verification"),
        "session_attempt": mocker.patch.object(
            module.conversation_state, "record_failed_attempt", return_value=1
        ),
        "attempts": mocker.patch.object(
            module.conversation_state, "record_factor_attempts", return_value={}
        ),
        "signal": mocker.patch.object(module.conversation_state, "record_risk_signal"),
        "module": module,
    }


def call(stubs, factors, conversation_id="conv_1", api_key=API_KEY, **extra):
    """Invokes the handler as API Gateway would."""
    body = {"conversation_id": conversation_id, "factors": factors, **extra}
    response = stubs["module"].handler(
        {"headers": {"x-api-key": api_key}, "body": json.dumps(body)}
    )
    return json.loads(response["body"])


def factor(field: Factor, value: str) -> dict:
    return {"field": field.value, "value": value}


class TestAuthentication:
    def test_a_wrong_api_key_is_refused(self, stubs):
        result = call(stubs, [], api_key="not-the-key")
        assert result["status"] == "SERVICE_UNAVAILABLE"
        assert result["error_category"] == "NOT_AUTHORIZED"

    def test_a_refused_call_never_touches_the_identity_table(self, stubs):
        call(stubs, [factor(Factor.EMAIL, RECORD["email"])], api_key="not-the-key")
        stubs["get"].assert_not_called()


class TestVerification:
    def test_three_correct_factors_verify_and_mark_the_conversation(self, stubs):
        result = call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, "CUST-00417"),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, "044 123 45 67"),
            ],
        )
        assert result["status"] == "VERIFIED"
        assert result["factors_confirmed"] == 3
        stubs["set_verification"].assert_called_once()

    def test_an_unverified_call_never_marks_the_conversation(self, stubs):
        call(stubs, [factor(Factor.EMAIL, RECORD["email"])])
        stubs["set_verification"].assert_not_called()

    def test_the_response_carries_a_field_name_and_never_a_value(self, stubs):
        result = call(stubs, [factor(Factor.CUSTOMER_ID, "CUST-00417")])
        assert result["next_factor_hint"] in {
            "email",
            "phone",
            "date_of_birth",
            "account_opening_year",
        }
        for stored_value in RECORD.values():
            assert str(stored_value) not in json.dumps(result)


class TestLockout:
    def test_a_wrong_answer_advances_the_customer_counter(self, stubs):
        call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, "CUST-00417"),
                factor(Factor.EMAIL, "wrong@example.com"),
            ],
        )
        assert stubs["update"].called

    def test_a_wrong_answer_advances_the_session_counter_even_with_no_account_named(self, stubs):
        """The hole the per-customer counter leaves: a caller who never names an account has
        no counter to exhaust and could otherwise guess indefinitely (FR-006)."""
        call(stubs, [factor(Factor.EMAIL, "wrong@example.com")])
        stubs["session_attempt"].assert_called_once()

    def test_the_session_lock_engages_without_any_account_being_named(self, stubs):
        stubs["session_attempt"].return_value = 3
        result = call(stubs, [factor(Factor.EMAIL, "wrong@example.com")])
        assert result["status"] == "LOCKED"

    def test_answering_too_few_questions_does_not_advance_the_counter(self, stubs):
        """A caller who knows two facts has not failed; counting this would lock out honest
        customers who cannot recall their account opening year (FR-006)."""
        call(stubs, [factor(Factor.EMAIL, RECORD["email"])])
        stubs["update"].assert_not_called()

    def test_the_final_wrong_attempt_locks_the_account(self, stubs):
        stubs["get"].return_value = {**RECORD, "failed_verification_attempts": 2}
        call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, "CUST-00417"),
                factor(Factor.EMAIL, "wrong@example.com"),
            ],
        )
        expression = stubs["update"].call_args.kwargs["UpdateExpression"]
        assert "locked_until" in expression

    def test_a_locked_account_is_refused_without_evaluating_answers(self, stubs):
        """Otherwise the lockout is a speed bump: a caller could keep guessing and read the
        confirmed count to learn which answers were right."""
        stubs["get"].return_value = {
            **RECORD,
            "locked_until": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        }
        result = call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, "CUST-00417"),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
            ],
        )
        assert result["status"] == "LOCKED"
        assert result["factors_confirmed"] == 0

    def test_an_expired_lock_no_longer_blocks(self, stubs):
        stubs["get"].return_value = {
            **RECORD,
            "locked_until": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }
        result = call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, "CUST-00417"),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
            ],
        )
        assert result["status"] == "VERIFIED"

    def test_verifying_clears_the_counter(self, stubs):
        stubs["get"].return_value = {**RECORD, "failed_verification_attempts": 2}
        call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, "CUST-00417"),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
            ],
        )
        assert "REMOVE locked_until" in stubs["update"].call_args.kwargs["UpdateExpression"]


class TestCustomerResolution:
    def test_a_supplied_customer_id_wins_over_the_caller_id_candidate(self, stubs):
        """The candidate exists to pick a greeting language. Letting it select the record
        when the caller names a different one would make the phone number a factor
        (FR-033b)."""
        call(
            stubs,
            [factor(Factor.CUSTOMER_ID, "CUST-00417")],
            candidate_customer_id="CUST-99999",
        )
        assert stubs["get"].call_args.args[1] == {"customer_id": "CUST-00417"}

    def test_the_candidate_may_scope_the_lookup_when_no_id_is_given(self, stubs):
        """Granting nothing: three correct factors are still required against whatever
        record is chosen."""
        call(
            stubs,
            [factor(Factor.EMAIL, RECORD["email"])],
            candidate_customer_id="CUST-00417",
        )
        assert stubs["get"].call_args.args[1] == {"customer_id": "CUST-00417"}

    def test_the_candidate_alone_never_verifies(self, stubs):
        result = call(stubs, [], candidate_customer_id="CUST-00417")
        assert result["status"] != "VERIFIED"
        assert result["factors_confirmed"] == 0


class TestUnknownCustomer:
    def test_an_unknown_customer_is_indistinguishable_from_wrong_answers(self, stubs):
        """A gate that answers 'no such customer' differently is a way to enumerate who is a
        customer."""
        stubs["get"].return_value = None
        unknown = call(
            stubs,
            [factor(Factor.CUSTOMER_ID, "CUST-99999"), factor(Factor.EMAIL, "a@b.ch")],
        )
        stubs["get"].return_value = dict(RECORD)
        wrong = call(
            stubs,
            [factor(Factor.CUSTOMER_ID, "CUST-99999"), factor(Factor.EMAIL, "a@b.ch")],
        )
        assert unknown["status"] == wrong["status"]
        assert unknown["factors_confirmed"] == wrong["factors_confirmed"]


class TestDegradation:
    def test_an_identity_store_outage_is_not_reported_as_a_failed_verification(self, stubs):
        """Principle III. 'Cannot check right now' and 'you answered wrongly' are different
        answers, and conflating them would lock out customers during an outage."""
        from src.adapters.errors import ErrorCategory, ToolError

        stubs["get"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "table down")
        result = call(stubs, [factor(Factor.CUSTOMER_ID, "CUST-00417")])
        assert result["status"] == "SERVICE_UNAVAILABLE"
        assert result["error_category"] == "DEPENDENCY_DOWN"
        assert result["retryable"] is True


class TestMalformedInput:
    def test_a_missing_conversation_id_is_a_validation_error(self, stubs):
        response = stubs["module"].handler(
            {"headers": {"x-api-key": API_KEY}, "body": json.dumps({"factors": []})}
        )
        assert json.loads(response["body"])["error_category"] == "VALIDATION"

    def test_an_invented_field_name_is_ignored_rather_than_fatal(self, stubs):
        """A language model will occasionally invent a field. Failing the call for it turns
        a harmless hallucination into a refused customer."""
        result = call(
            stubs,
            [
                {"field": "favourite_colour", "value": "blue"},
                factor(Factor.CUSTOMER_ID, "CUST-00417"),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
            ],
        )
        assert result["status"] == "VERIFIED"


class TestGuessing:
    """Enumeration is stopped by the backend, not by the agent noticing (FR-006d)."""

    def test_a_third_distinct_value_for_one_field_locks_the_call(self, stubs):
        stubs["attempts"].return_value = {"customer_id": 3}
        result = call(stubs, [factor(Factor.CUSTOMER_ID, "CUST-00003")])
        assert result["status"] == "LOCKED"

    def test_it_locks_before_the_answers_are_evaluated(self, stubs):
        """A caller working through values must not learn from the attempt that stops them
        whether that one was right."""
        stubs["attempts"].return_value = {"customer_id": 3}
        result = call(stubs, [factor(Factor.CUSTOMER_ID, "CUST-00417")])
        assert result["status"] == "LOCKED"
        assert result["factors_confirmed"] == 0

    def test_a_risk_signal_is_raised(self, stubs):
        stubs["attempts"].return_value = {"customer_id": 3}
        call(stubs, [factor(Factor.CUSTOMER_ID, "CUST-00003")])
        signal = stubs["signal"].call_args.args[0]
        assert signal.signal_type == "SUSPECTED_GUESSING"

    def test_the_signal_records_a_count_and_never_the_values_offered(self, stubs):
        """Recording the guesses would defeat the point of fingerprinting them."""
        stubs["attempts"].return_value = {"customer_id": 3}
        call(stubs, [factor(Factor.CUSTOMER_ID, "CUST-SECRET-GUESS")])
        assert "CUST-SECRET-GUESS" not in stubs["signal"].call_args.args[0].evidence

    def test_two_values_for_one_field_is_a_correction_not_enumeration(self, stubs):
        stubs["attempts"].return_value = {"customer_id": 2}
        result = call(stubs, [factor(Factor.CUSTOMER_ID, "CUST-00417")])
        assert result["status"] != "LOCKED"

    def test_corrections_across_different_fields_do_not_accumulate(self, stubs):
        stubs["attempts"].return_value = {"customer_id": 2, "email": 2, "phone": 2}
        result = call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, "CUST-00417"),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
            ],
        )
        assert result["status"] == "VERIFIED"

    def test_only_fingerprints_are_recorded_never_the_answers(self, stubs):
        call(stubs, [factor(Factor.EMAIL, "klaus@example.ch")])
        recorded = stubs["attempts"].call_args.args[1]
        assert "klaus@example.ch" not in str(recorded)
        assert set(recorded) == {"email"}


class TestConflictingIdentityData:
    def test_naming_a_different_account_than_the_caller_id_raises_a_signal(self, stubs):
        """Innocent explanations exist — a shared switchboard, a colleague's desk — so it is
        recorded rather than acted on."""
        call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, "CUST-00417"),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
            ],
            candidate_customer_id="CUST-99999",
        )
        types = [c.args[0].signal_type for c in stubs["signal"].call_args_list]
        assert "CONFLICTING_IDENTITY_DATA" in types

    def test_no_signal_when_the_caller_id_agrees(self, stubs):
        call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, "CUST-00417"),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
            ],
            candidate_customer_id="CUST-00417",
        )
        types = [c.args[0].signal_type for c in stubs["signal"].call_args_list]
        assert "CONFLICTING_IDENTITY_DATA" not in types


class TestResolvingTheCustomer:
    """A caller must be findable by whatever identifier they actually know.

    The bug this class exists for: resolution used only the customer id, so a caller who led
    with their email had that correct answer scored as wrong, and it burned a lockout
    attempt. Verification could not progress until they recited an id. Found by walking
    through the conversation, not by any test — the integration test sends all three factors
    at once, which is not how a conversation works.
    """

    def test_an_email_alone_resolves_the_account_and_confirms(self, stubs):
        stubs["query"].return_value = [{"customer_id": "CUST-00417"}]
        result = call(stubs, [factor(Factor.EMAIL, RECORD["email"])])
        assert result["status"] == "PARTIALLY_VERIFIED"
        assert result["factors_confirmed"] == 1

    def test_a_phone_alone_resolves_the_account(self, stubs):
        stubs["query"].return_value = [{"customer_id": "CUST-00417"}]
        result = call(stubs, [factor(Factor.PHONE, "044 123 45 67")])
        assert result["factors_confirmed"] == 1

    def test_a_correct_answer_given_first_is_never_scored_as_wrong(self, stubs):
        """The heart of the bug. A correct email must not count as a failed attempt."""
        stubs["query"].return_value = [{"customer_id": "CUST-00417"}]
        call(stubs, [factor(Factor.EMAIL, RECORD["email"])])
        stubs["session_attempt"].assert_not_called()

    def test_a_supplied_customer_id_still_wins_over_a_lookup(self, stubs):
        stubs["query"].return_value = [{"customer_id": "CUST-99999"}]
        call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, "CUST-00417"),
                factor(Factor.EMAIL, RECORD["email"]),
            ],
        )
        assert stubs["get"].call_args.args[1] == {"customer_id": "CUST-00417"}

    def test_an_email_that_matches_nobody_looks_like_a_wrong_answer(self, stubs):
        """Resolution failing and an answer being wrong must be the same response, or the
        gate says whether an address is on file."""
        stubs["query"].return_value = []
        result = call(stubs, [factor(Factor.EMAIL, "nobody@example.invalid")])
        assert result["status"] == "FAILED"
        assert result["factors_confirmed"] == 0

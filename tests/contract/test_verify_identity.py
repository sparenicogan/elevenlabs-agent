"""The verify_identity endpoint (contracts/tools.md).

Covers what the handler does that the pure rule cannot: authentication, the lockout
counter, resolving whose record to check, and the shape of what goes back on the wire.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.domain.verification import Factor

API_KEY = "test-key"

# One person, belonging to one company account. Financial records are keyed by account_id;
# this row is keyed by the person.
RECORD = {
    "contact_id": "859557757171",
    "account_id": "445909044455",
    "company_name": "Alpina Tech",
    "first_name": "Klaus",
    "last_name": "Mueller",
    "email": "klaus.mueller@alpina-tech.ch",
    "phone": "+41 44 501 22 18",
    "date_of_birth": "1974-03-12",
    "failed_verification_attempts": 0,
}

# What the email and phone indexes return: a pointer to the person, nothing else.
LOOKUP_HIT = [{"contact_id": RECORD["contact_id"]}]


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
        # Resolution by email or phone. A caller is found by something personal — the
        # customer id names a company and cannot identify a person.
        "query": mocker.patch.object(module.dynamo, "query", return_value=list(LOOKUP_HIT)),
        "update": mocker.patch.object(module.dynamo, "update_if", return_value={}),
        "set_verification": mocker.patch.object(module.conversation_state, "set_verification"),
        "session_attempt": mocker.patch.object(
            module.conversation_state, "record_failed_attempt", return_value=1
        ),
        "attempts": mocker.patch.object(
            module.conversation_state, "record_factor_attempts", return_value=({}, True)
        ),
        "signal": mocker.patch.object(module.conversation_state, "record_risk_signal"),
        # Distinct wrong values seen so far in the call. Zero unless a test says otherwise.
        "wrong": mocker.patch.object(
            module.conversation_state, "record_wrong_values", return_value=0
        ),
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
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, "044 501 22 18"),
            ],
        )
        assert result["status"] == "VERIFIED"
        assert result["factors_confirmed"] == 3
        stubs["set_verification"].assert_called_once()

    def test_an_unverified_call_never_marks_the_conversation(self, stubs):
        call(stubs, [factor(Factor.EMAIL, RECORD["email"])])
        stubs["set_verification"].assert_not_called()

    def test_the_response_carries_a_field_name_and_never_a_value(self, stubs):
        result = call(stubs, [factor(Factor.CUSTOMER_ID, RECORD["account_id"])])
        assert result["next_factor_hint"] in {
            "email",
            "phone",
            "date_of_birth",
        }
        echoed_back = {RECORD["account_id"], RECORD["contact_id"]}
        for field, stored_value in RECORD.items():
            if stored_value in echoed_back or field == "failed_verification_attempts":
                continue
            assert str(stored_value) not in json.dumps(result), field


class TestLockout:
    def test_a_wrong_answer_advances_the_customer_counter(self, stubs):
        stubs["wrong"].return_value = 1
        call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
                factor(Factor.EMAIL, "wrong@example.com"),
            ],
        )
        assert stubs["update"].called

    def test_a_wrong_answer_is_counted_even_with_no_account_named(self, stubs):
        """The hole the per-customer counter leaves: a caller who never names an account has
        no counter to exhaust and could otherwise guess indefinitely (FR-006)."""
        call(stubs, [factor(Factor.EMAIL, "wrong@example.com")])
        stubs["wrong"].assert_called_once()
        assert stubs["wrong"].call_args.args[1], "the wrong value should be fingerprinted"

    def test_the_lock_engages_without_any_account_being_named(self, stubs):
        stubs["wrong"].return_value = 3
        result = call(stubs, [factor(Factor.EMAIL, "wrong@example.com")])
        assert result["status"] == "LOCKED"

    def test_answering_too_few_questions_does_not_advance_the_counter(self, stubs):
        """A caller who knows two facts has not failed; counting this would lock out honest
        customers who cannot recall their account opening year (FR-006)."""
        call(stubs, [factor(Factor.EMAIL, RECORD["email"])])
        stubs["update"].assert_not_called()

    def test_the_final_wrong_attempt_locks_the_account(self, stubs):
        stubs["wrong"].return_value = 3
        stubs["get"].return_value = {**RECORD, "failed_verification_attempts": 2}
        call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
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
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
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
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
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
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
            ],
        )
        assert "REMOVE locked_until" in stubs["update"].call_args.kwargs["UpdateExpression"]


class TestResolutionNeedsSomethingPersonal:
    """A customer id names a company, so it cannot identify a person.

    A caller who offers only their customer id has said which company they are calling about
    and nothing about who they are. Resolution therefore needs an email or a phone number —
    which is also the first thing the agent asks for.
    """

    def test_a_customer_id_alone_resolves_nobody(self, stubs):
        stubs["query"].return_value = []
        result = call(stubs, [factor(Factor.CUSTOMER_ID, RECORD["account_id"])])
        assert result["status"] == "FAILED"
        assert result["factors_confirmed"] == 0

    def test_an_email_resolves_the_person(self, stubs):
        call(stubs, [factor(Factor.EMAIL, RECORD["email"])])
        assert stubs["get"].call_args.args[1] == {"contact_id": RECORD["contact_id"]}

    def test_the_customer_id_is_checked_against_the_persons_company(self, stubs):
        """It is a real factor — it just cannot be the one that finds them."""
        result = call(
            stubs,
            [
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
            ],
        )
        assert result["status"] == "VERIFIED"

    def test_naming_the_wrong_company_does_not_verify(self, stubs):
        """Anna Schmidt is real and her details are right, but she does not work at the
        company she named."""
        result = call(
            stubs,
            [
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
                factor(Factor.CUSTOMER_ID, "CUST-99999"),
            ],
        )
        assert result["status"] == "FAILED"


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
        result = call(stubs, [factor(Factor.EMAIL, RECORD["email"])])
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
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
            ],
        )
        assert result["status"] == "VERIFIED"


class TestGuessing:
    """Enumeration is stopped by the backend, not by the agent noticing (FR-006d)."""

    def test_a_third_distinct_value_for_one_field_locks_the_call(self, stubs):
        stubs["attempts"].return_value = ({"customer_id": 3}, True)
        result = call(stubs, [factor(Factor.CUSTOMER_ID, "445900000003")])
        assert result["status"] == "LOCKED"

    def test_it_locks_before_the_answers_are_evaluated(self, stubs):
        """A caller working through values must not learn from the attempt that stops them
        whether that one was right."""
        stubs["attempts"].return_value = ({"customer_id": 3}, True)
        result = call(stubs, [factor(Factor.CUSTOMER_ID, RECORD["account_id"])])
        assert result["status"] == "LOCKED"
        assert result["factors_confirmed"] == 0

    def test_a_risk_signal_is_raised(self, stubs):
        stubs["attempts"].return_value = ({"customer_id": 3}, True)
        call(stubs, [factor(Factor.CUSTOMER_ID, "445900000003")])
        signal = stubs["signal"].call_args.args[0]
        assert signal.signal_type == "SUSPECTED_GUESSING"

    def test_the_signal_records_a_count_and_never_the_values_offered(self, stubs):
        """Recording the guesses would defeat the point of fingerprinting them."""
        stubs["attempts"].return_value = ({"customer_id": 3}, True)
        call(stubs, [factor(Factor.CUSTOMER_ID, "CUST-SECRET-GUESS")])
        assert "CUST-SECRET-GUESS" not in stubs["signal"].call_args.args[0].evidence

    def test_two_values_for_one_field_is_a_correction_not_enumeration(self, stubs):
        stubs["attempts"].return_value = ({"customer_id": 2}, True)
        result = call(stubs, [factor(Factor.CUSTOMER_ID, RECORD["account_id"])])
        assert result["status"] != "LOCKED"

    def test_corrections_across_different_fields_do_not_accumulate(self, stubs):
        stubs["attempts"].return_value = ({"customer_id": 2, "email": 2, "phone": 2}, True)
        result = call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
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
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
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
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
            ],
            candidate_customer_id=RECORD["contact_id"],
        )
        types = [c.args[0].signal_type for c in stubs["signal"].call_args_list]
        assert "CONFLICTING_IDENTITY_DATA" not in types


class TestResendingIsNotRetrying:
    """A caller who mistypes one answer has it resent on every subsequent turn, because the
    agent sends everything it has gathered. Counting failed *calls* locks them out three
    turns after a single typo, however correct everything after it is.

    Found by an adversarial walkthrough: one wrong email produced three failed attempts and
    a lockout while the caller's next two answers were both right. The fix is to count
    distinct wrong *values*.
    """

    def test_only_the_wrong_answers_are_fingerprinted(self, stubs):
        """The correct ones are not counted against the caller, however often they arrive."""
        call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
                factor(Factor.EMAIL, "wrong@example.com"),
            ],
        )
        assert len(stubs["wrong"].call_args.args[1]) == 1

    def test_a_correct_attempt_fingerprints_nothing(self, stubs):
        call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
            ],
        )
        assert stubs["wrong"].call_args.args[1] == set()

    def test_the_same_wrong_value_fingerprints_identically_every_turn(self, stubs):
        """Which is what makes the set converge rather than grow. One typo is one strike,
        however many turns carry it."""
        seen = []
        for _ in range(3):
            call(stubs, [factor(Factor.EMAIL, "wrong@example.com")])
            seen.append(stubs["wrong"].call_args.args[1])
        assert seen[0] == seen[1] == seen[2]
        assert len(seen[0]) == 1

    def test_two_different_wrong_values_fingerprint_differently(self, stubs):
        call(stubs, [factor(Factor.EMAIL, "first-wrong@example.com")])
        first = stubs["wrong"].call_args.args[1]
        call(stubs, [factor(Factor.EMAIL, "second-wrong@example.com")])
        assert stubs["wrong"].call_args.args[1] != first

    def test_the_lock_engages_on_the_third_distinct_wrong_value(self, stubs):
        stubs["wrong"].return_value = 3
        assert call(stubs, [factor(Factor.EMAIL, "c@example.com")])["status"] == "LOCKED"

    def test_two_distinct_wrong_values_do_not_lock(self, stubs):
        stubs["wrong"].return_value = 2
        assert call(stubs, [factor(Factor.EMAIL, "b@example.com")])["status"] != "LOCKED"

    def test_a_caller_who_corrects_a_typo_can_still_verify(self, stubs):
        """The scenario from the walkthrough. One wrong email, then the right answers."""
        stubs["wrong"].return_value = 1
        result = call(
            stubs,
            [
                factor(Factor.CUSTOMER_ID, RECORD["account_id"]),
                factor(Factor.EMAIL, RECORD["email"]),
                factor(Factor.PHONE, RECORD["phone"]),
            ],
        )
        assert result["status"] == "VERIFIED"

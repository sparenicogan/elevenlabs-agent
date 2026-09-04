"""US6: choosing the language before anyone has spoken.

The webhook answers one question and must never fail the call answering it. Everything it
returns is a convenience; nothing it returns is evidence.
"""

import json

import pytest

RECORD = {
    "contact_id": "859557757171",
    "account_id": "445909044455",
    "preferred_language": "it",
}


@pytest.fixture
def stubs(mocker):
    from src.handlers import conversation_init as module

    return {
        "query": mocker.patch.object(
            module.dynamo, "query", return_value=[{"contact_id": RECORD["contact_id"]}]
        ),
        "get": mocker.patch.object(module.dynamo, "get", return_value=dict(RECORD)),
        "module": module,
    }


def call(stubs, caller_id="+41 91 604 77 31"):
    """The response body, with the dynamic variables lifted alongside it for readability."""
    response = stubs["module"].handler({"body": json.dumps({"caller_id": caller_id})})
    body = json.loads(response["body"])
    return {**body, **body["dynamic_variables"]}


class TestAKnownNumber:
    def test_a_known_number_yields_its_customers_language(self, stubs):
        assert call(stubs)["greeting_language"] == "it"

    def test_the_override_matches_the_greeting(self, stubs):
        body = call(stubs)
        assert body["conversation_config_override"]["agent"]["language"] == "it"

    def test_the_candidate_is_passed_as_a_secret_variable(self, stubs):
        """So it stays out of the transcript and the model's visible context."""
        variables = call(stubs)["dynamic_variables"]
        assert variables["secret__candidate_customer_id"] == "445909044455"
        assert not any(k.startswith("candidate") for k in variables)

    def test_the_number_is_normalised_the_same_way_everywhere(self, stubs):
        """A number arriving here and the same number spoken aloud must resolve identically,
        or the greeting is right and the verification is not."""
        from src.domain.verification import Factor, lookup_key

        call(stubs, caller_id="0916047731")
        queried = stubs["query"].call_args.kwargs["KeyConditionExpression"]
        assert queried.get_expression()["values"][1] == lookup_key(Factor.PHONE, "+41 91 604 77 31")


class TestAnUnknownNumber:
    def test_it_falls_back_to_german(self, stubs):
        """FR-033a. The largest part of the customer base, and what a Swiss caller is least
        surprised by."""
        stubs["query"].return_value = []
        assert call(stubs)["greeting_language"] == "de"

    def test_no_candidate_is_passed(self, stubs):
        stubs["query"].return_value = []
        assert call(stubs)["dynamic_variables"]["secret__candidate_customer_id"] is None

    def test_a_number_matching_two_people_resolves_nobody(self, stubs):
        """A shared switchboard is ordinary. Picking one of them would hand the wrong account
        to the next question."""
        stubs["query"].return_value = [{"contact_id": "a"}, {"contact_id": "b"}]
        assert call(stubs)["dynamic_variables"]["secret__candidate_customer_id"] is None


class TestItNeverFailsTheCall:
    def test_an_unreachable_table_still_answers(self, stubs):
        """A caller hearing German when they expected French is a small annoyance. A caller
        hearing nothing is a lost call."""
        from src.adapters.errors import ErrorCategory, ToolError

        stubs["query"].side_effect = ToolError(ErrorCategory.DEPENDENCY_DOWN, "down")
        body = call(stubs)
        assert body["greeting_language"] == "de"

    def test_a_request_with_no_caller_id_still_answers(self, stubs):
        assert call(stubs, caller_id="")["greeting_language"] == "de"

    def test_a_malformed_body_still_answers(self, stubs):
        response = stubs["module"].handler({"body": "not json"})
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["dynamic_variables"]["greeting_language"] == "de"


class TestItGrantsNothing:
    def test_a_regional_code_is_reduced_to_its_language(self, stubs):
        stubs["get"].return_value = {**RECORD, "preferred_language": "fr-CH"}
        assert call(stubs)["greeting_language"] == "fr"

    def test_an_unsupported_language_falls_back(self, stubs):
        stubs["get"].return_value = {**RECORD, "preferred_language": "es"}
        assert call(stubs)["greeting_language"] == "de"

    def test_the_candidate_is_not_a_verification_factor(self):
        """FR-033b. verify_identity re-derives the customer from what the caller says; a
        mismatch is a risk signal, not an authorisation."""
        import inspect

        from src.handlers import verify_identity

        resolve = inspect.getsource(verify_identity._resolve_contact)
        # The candidate is consulted only after every supplied identifier has failed.
        assert resolve.index("candidate_customer_id") > resolve.index("lookup_contact")

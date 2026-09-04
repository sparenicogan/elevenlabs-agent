"""The check_factor endpoint.

It exists for what a voice call loses. A Swiss surname arrives misspelled and a spoken date
means two different days depending on who transcribed it -- both recoverable while the caller
is still on that question, neither recoverable afterwards.
"""

import json

import pytest

API_KEY = "test-key"

RECORD = {
    "contact_id": "859554863314",
    "account_id": "445900025039",
    "email": "charles.lavigne@apex-capital.ch",
    "phone": "+41 22 704 51 88",
    "date_of_birth": "1968-09-14",
}


@pytest.fixture
def stubs(mocker):
    from src.handlers import check_factor as module

    mocker.patch.object(module.secrets, "get", return_value=API_KEY)
    return {
        "lookup": mocker.patch.object(
            module.identity, "lookup_contact", return_value=RECORD["contact_id"]
        ),
        "load": mocker.patch.object(module.identity, "load_record", return_value=dict(RECORD)),
        "remember": mocker.patch.object(module.conversation_state, "set_resolved_contact"),
        "resolved": mocker.patch.object(
            module.conversation_state, "resolved_contact", return_value=RECORD["contact_id"]
        ),
        "module": module,
    }


def call(stubs, field, value, api_key=API_KEY):
    response = stubs["module"].handler(
        {
            "headers": {"x-api-key": api_key},
            "body": json.dumps({"conversation_id": "conv_1", "field": field, "value": value}),
        }
    )
    return json.loads(response["body"])


class TestEmail:
    def test_a_correct_address_matches(self, stubs):
        assert call(stubs, "email", RECORD["email"])["status"] == "MATCHED"

    def test_a_misheard_surname_does_not(self, stubs):
        """Lavigne heard as Levine. This is what the spelling recovery is for."""
        stubs["lookup"].return_value = None
        stubs["resolved"].return_value = None
        assert call(stubs, "email", "charles.levine@apex-capital.ch")["status"] == "NOT_MATCHED"

    def test_resolving_is_remembered_for_the_rest_of_the_call(self, stubs):
        """A date of birth cannot be looked up, so checking one needs a record already
        in hand."""
        call(stubs, "email", RECORD["email"])
        stubs["remember"].assert_called_once_with("conv_1", RECORD["contact_id"])

    def test_it_never_returns_the_stored_value(self, stubs):
        stubs["lookup"].return_value = None
        stubs["resolved"].return_value = None
        body = json.dumps(call(stubs, "email", "guess@apex-capital.ch"))
        for value in RECORD.values():
            assert value not in body


class TestDateOfBirth:
    def test_an_ambiguous_date_is_flagged_before_anything_is_looked_up(self, stubs):
        """ "11 6 1994" is the 11th of June or the 6th of November, and the string does not
        say which. Asking is a question about the transcription, not about the caller, so it
        happens without touching the record."""
        result = call(stubs, "date_of_birth", "11 6 1994")
        assert result["status"] == "AMBIGUOUS"
        assert result["readings"] == ["1994-06-11", "1994-11-06"]
        stubs["load"].assert_not_called()

    def test_an_unambiguous_date_is_simply_checked(self, stubs):
        assert call(stubs, "date_of_birth", "1968-09-14")["status"] == "MATCHED"

    def test_a_day_over_twelve_is_not_ambiguous(self, stubs):
        """25.12.1980 can only be read one way."""
        assert call(stubs, "date_of_birth", "25.12.1980")["status"] != "AMBIGUOUS"

    def test_it_needs_a_record_resolved_earlier_in_the_call(self, stubs):
        stubs["resolved"].return_value = None
        assert call(stubs, "date_of_birth", "1968-09-14")["status"] == "NOT_MATCHED"


class TestItDecidesNothing:
    def test_a_match_does_not_verify_the_conversation(self, stubs):
        """Every detail can match here and the caller is still not through the gate.
        verify_identity makes that decision and this endpoint cannot reach it."""
        module = stubs["module"]
        assert not hasattr(module, "set_verification")
        assert "set_verification" not in dir(module.conversation_state.__class__)

    def test_an_unchecked_field_is_refused(self, stubs):
        assert call(stubs, "customer_id", "445900025039")["error_category"] == "VALIDATION"

    def test_a_bad_api_key_is_refused(self, stubs):
        assert call(stubs, "email", RECORD["email"], api_key="wrong")["status"] != "MATCHED"

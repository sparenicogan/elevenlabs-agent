"""What a person receives when a call is handed to them (FR-019).

Composed in the backend rather than by the model, because the field set is fixed by
requirement and a model summarising under time pressure drops whatever it judges least
important — which is not a judgement it is in a position to make.

The measure of a handoff is whether the caller has to explain themselves twice.
"""

import pytest

from src.domain.handoff import compose_handoff

CONVERSATION = "conv_1"
PROBLEM = "an invoice for four thousand two hundred that we already paid"


def handoff(**overrides) -> str:
    defaults = {
        "reason": "PAYMENT_UNVERIFIABLE",
        "verified": True,
        "conversation_id": CONVERSATION,
        "company_name": "Alpina Tech",
        "caller_stated_problem": PROBLEM,
    }
    return compose_handoff(**{**defaults, **overrides})


class TestEveryHandoffCarriesTheRequiredFields:
    def test_it_names_the_reason(self):
        assert "payment unverifiable" in handoff().lower()

    def test_it_states_the_verification_outcome(self):
        assert "verified" in handoff().lower()

    def test_it_carries_the_conversation_so_the_recording_can_be_found(self):
        assert CONVERSATION in handoff()

    def test_it_carries_what_the_caller_said_in_their_own_words(self):
        assert PROBLEM in handoff()

    def test_it_says_explicitly_that_they_have_explained_once(self):
        """The instruction to the person picking up, not decoration. A handoff that lists
        facts without saying what to do with them gets skimmed."""
        assert "should not have to again" in handoff()


class TestVerifiedAndUnverifiedDiffer:
    def test_a_verified_handoff_names_the_customer(self):
        assert "Alpina Tech" in handoff()

    def test_an_unverified_handoff_says_so_before_anything_else(self):
        """A reader skimming must not assume identity was established, because everything
        after it is a claim rather than a fact."""
        text = handoff(verified=False, company_name=None, reason="IDENTITY_NOT_ESTABLISHED")
        assert "NOT VERIFIED" in text
        assert text.index("NOT VERIFIED") < text.index(PROBLEM)

    def test_an_unverified_handoff_never_asserts_a_customer(self):
        text = handoff(verified=False, company_name=None, reason="IDENTITY_NOT_ESTABLISHED")
        assert "Caller verified as" not in text

    def test_a_self_description_is_carried_and_marked_as_theirs(self):
        text = handoff(
            verified=False,
            company_name=None,
            reason="IDENTITY_NOT_ESTABLISHED",
            caller_self_description="I'm the finance director",
        )
        assert "They said of themselves" in text
        assert "finance director" in text


class TestOptionalContext:
    def test_a_discrepancy_is_reported_with_what_the_caller_said_about_it(self):
        text = handoff(discrepancy={"field": "payer_address", "caller_explanation": "MOVED"})
        assert "payer_address" in text
        assert "MOVED" in text

    def test_risk_signals_reach_the_person_taking_over(self):
        text = handoff(risk_signals=["SUSPECTED_GUESSING", "SUSPECTED_GUESSING"])
        assert "SUSPECTED_GUESSING" in text

    def test_repeated_signals_are_not_repeated_in_the_text(self):
        text = handoff(risk_signals=["SUSPECTED_GUESSING"] * 3)
        assert text.count("SUSPECTED_GUESSING") == 1

    @pytest.mark.parametrize("field", ["caller_stated_problem", "notes", "caller_self_description"])
    def test_an_empty_field_is_omitted_rather_than_printed_blank(self, field):
        text = handoff(**{field: None})
        assert "None" not in text
        assert ": \n" not in text


class TestItIsReadableAtSpeed:
    def test_it_is_short_enough_to_take_in_before_answering(self):
        """A person has seconds between accepting a transfer and speaking. Anything longer
        gets skimmed, and a skimmed handoff is the same as none."""
        assert len(handoff().split("\n")) <= 8

    def test_the_reason_is_the_first_thing_they_read(self):
        assert handoff().split("\n")[0].startswith("Escalation:")

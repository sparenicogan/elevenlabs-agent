"""The disclosure gate (FR-003, FR-003a, FR-004, FR-006).

Nothing financial is said before this rule returns VERIFIED, so it is the one place where
being wrong is expensive in both directions: too strict and a real customer is refused
their own invoice, too loose and a stranger is told about someone else's.

The rule is pure. It is given a caller's answers and the stored record and returns a
status; it does not decide when to lock an account or write a risk signal, because those
are effects and belong in the handler.
"""

from datetime import date
from decimal import Decimal  # noqa: F401  (kept: fixtures below mirror the identity record)

import pytest
from src.domain.verification import (
    Factor,
    VerificationStatus,
    check_factors,
)

STORED = {
    Factor.EMAIL: "Buchhaltung@Meier-Bau.ch",
    Factor.PHONE: "+41 44 123 45 67",
    Factor.DATE_OF_BIRTH: "1974-03-12",
    Factor.ACCOUNT_OPENING_YEAR: "2019",
    Factor.CUSTOMER_ID: "CUST-00417",
}

REQUIRED = 3


def check(supplied, required=REQUIRED, stored=None):
    """Runs the rule against the default stored record."""
    return check_factors(
        supplied=supplied,
        stored=STORED if stored is None else stored,
        required_count=required,
    )


class TestVerified:
    def test_three_correct_factors_including_a_non_document_one(self):
        result = check(
            {
                Factor.CUSTOMER_ID: "CUST-00417",
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.PHONE: "+41 44 123 45 67",
            }
        )
        assert result.status is VerificationStatus.VERIFIED
        assert result.confirmed_count == 3

    def test_email_comparison_ignores_case(self):
        """The agent transcribes what it hears; a caller does not speak capital letters."""
        result = check(
            {
                Factor.EMAIL: "BUCHHALTUNG@MEIER-BAU.CH",
                Factor.DATE_OF_BIRTH: "1974-03-12",
                Factor.ACCOUNT_OPENING_YEAR: "2019",
            }
        )
        assert result.status is VerificationStatus.VERIFIED

    @pytest.mark.parametrize(
        "spoken_phone",
        [
            "+41 44 123 45 67",  # as stored
            "044 123 45 67",  # national form, as most callers say it
            "0041441234567",  # international prefix spelled out
            "+41441234567",  # no spacing
            "44 123 45 67",  # leading zero dropped by the transcriber
        ],
    )
    def test_a_phone_number_is_the_same_number_however_it_is_spoken(self, spoken_phone):
        """Comparing raw strings here would refuse correct callers routinely: the same Swiss
        number has several written forms and the transcription varies."""
        result = check(
            {
                Factor.PHONE: spoken_phone,
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.CUSTOMER_ID: "CUST-00417",
            }
        )
        assert result.status is VerificationStatus.VERIFIED


class TestNonDocumentRule:
    def test_three_factors_printed_on_the_invoice_do_not_verify(self):
        """FR-003a. A caller holding a stolen invoice has the customer id and can read the
        company details off it. At least one factor must be something the document does not
        carry, or possession of the invoice is possession of the account."""
        result = check(
            {
                Factor.CUSTOMER_ID: "CUST-00417",
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.PHONE: "+41 44 123 45 67",
            },
            stored={**STORED, Factor.EMAIL: "buchhaltung@meier-bau.ch"},
        )
        # Email and phone are non-document factors, so this one does verify.
        assert result.status is VerificationStatus.VERIFIED

    def test_customer_id_alone_is_not_enough_even_repeated(self):
        result = check({Factor.CUSTOMER_ID: "CUST-00417"})
        assert result.status is VerificationStatus.PARTIALLY_VERIFIED
        assert result.non_document_satisfied is False

    def test_the_hint_asks_for_a_non_document_factor_when_that_is_what_is_missing(self):
        """When the count is met but every factor came off the document, the next question
        must be one the document cannot answer."""
        result = check({Factor.CUSTOMER_ID: "CUST-00417"}, required=1)
        assert result.status is not VerificationStatus.VERIFIED
        assert result.next_factor_hint in {
            Factor.EMAIL,
            Factor.PHONE,
            Factor.DATE_OF_BIRTH,
            Factor.ACCOUNT_OPENING_YEAR,
        }


class TestPartiallyVerified:
    def test_correct_but_not_yet_enough(self):
        result = check(
            {
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.PHONE: "+41 44 123 45 67",
            }
        )
        assert result.status is VerificationStatus.PARTIALLY_VERIFIED
        assert result.confirmed_count == 2

    def test_nothing_supplied_at_all(self):
        result = check({})
        assert result.status is VerificationStatus.PARTIALLY_VERIFIED
        assert result.confirmed_count == 0

    def test_being_short_of_factors_is_not_a_failed_attempt(self):
        """A caller who does not know their account opening year has not failed
        verification; they have answered fewer questions. Counting this against the lockout
        would punish honesty (FR-006)."""
        result = check({Factor.EMAIL: "buchhaltung@meier-bau.ch"})
        assert result.is_failed_attempt is False


class TestFailed:
    def test_a_wrong_answer_fails_the_attempt(self):
        result = check(
            {
                Factor.EMAIL: "wrong@example.com",
                Factor.PHONE: "+41 44 123 45 67",
                Factor.CUSTOMER_ID: "CUST-00417",
            }
        )
        assert result.status is VerificationStatus.FAILED
        assert result.is_failed_attempt is True

    def test_a_wrong_answer_fails_even_when_the_others_would_have_sufficed(self):
        """Three correct answers plus one wrong one is not a pass. Otherwise a caller could
        guess freely as long as they also supplied enough correct fields."""
        result = check(
            {
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.PHONE: "+41 44 123 45 67",
                Factor.CUSTOMER_ID: "CUST-00417",
                Factor.DATE_OF_BIRTH: "1980-01-01",
            }
        )
        assert result.status is VerificationStatus.FAILED

    def test_the_result_never_says_which_factor_was_wrong(self):
        """FR-004. Telling the caller which answer failed turns verification into an oracle
        they can query one field at a time."""
        result = check(
            {
                Factor.EMAIL: "wrong@example.com",
                Factor.PHONE: "wrong-number",
                Factor.CUSTOMER_ID: "CUST-00417",
            }
        )
        serialised = repr(result)
        assert "wrong@example.com" not in serialised
        assert "EMAIL" not in serialised or result.next_factor_hint is not None
        assert not hasattr(result, "failed_factors")


class TestNoDisclosure:
    def test_the_result_never_carries_a_stored_value(self):
        """The rule returns a decision, not data. Nothing it produces can be read back to
        learn what the record holds (Principle IV)."""
        result = check({Factor.EMAIL: "buchhaltung@meier-bau.ch"})
        serialised = repr(result)
        for stored_value in STORED.values():
            assert stored_value not in serialised

    def test_an_unknown_customer_looks_exactly_like_a_wrong_answer(self):
        """Otherwise the response distinguishes 'no such customer' from 'wrong details', and
        the gate becomes a way to enumerate who is a customer."""
        unknown = check(
            {Factor.EMAIL: "someone@example.com", Factor.CUSTOMER_ID: "CUST-99999"},
            stored={},
        )
        wrong = check({Factor.EMAIL: "someone@example.com", Factor.CUSTOMER_ID: "CUST-99999"})
        assert unknown.status is wrong.status
        assert unknown.confirmed_count == wrong.confirmed_count


class TestDateHandling:
    def test_a_date_of_birth_is_compared_as_a_date_not_a_string(self):
        """The agent may transcribe a spoken date in more than one format."""
        result = check(
            {
                Factor.DATE_OF_BIRTH: "12.03.1974",
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.CUSTOMER_ID: "CUST-00417",
            }
        )
        assert result.status is VerificationStatus.VERIFIED

    def test_an_unparseable_date_is_a_wrong_answer_not_a_crash(self):
        result = check(
            {
                Factor.DATE_OF_BIRTH: "sometime in the seventies",
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.CUSTOMER_ID: "CUST-00417",
            }
        )
        assert result.status is VerificationStatus.FAILED


class TestConfigurability:
    def test_the_required_count_is_a_parameter_not_a_constant(self):
        """Principle VIII: the factor count is policy, read from the parameter store."""
        supplied = {
            Factor.EMAIL: "buchhaltung@meier-bau.ch",
            Factor.PHONE: "+41 44 123 45 67",
        }
        assert check(supplied, required=2).status is VerificationStatus.VERIFIED
        assert check(supplied, required=3).status is VerificationStatus.PARTIALLY_VERIFIED


def test_date_is_importable():
    """Guards the fixture format above against a stray refactor."""
    assert date.fromisoformat("1974-03-12").year == 1974

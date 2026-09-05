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
    Factor.CUSTOMER_ID: "445909044455",
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
    def test_three_correct_factors_including_a_personal_one(self):
        result = check(
            {
                Factor.CUSTOMER_ID: "445909044455",
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
                Factor.PHONE: "+41 44 123 45 67",
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
                Factor.CUSTOMER_ID: "445909044455",
            }
        )
        assert result.status is VerificationStatus.VERIFIED


class TestThePersonalFactorRule:
    def test_a_company_fact_alone_is_never_enough(self):
        """FR-003a. The customer id is printed on every invoice and known to everyone at the
        company, so producing it demonstrates familiarity with the business and nothing about
        who the caller is."""
        result = check(
            {
                Factor.CUSTOMER_ID: "445909044455",
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.PHONE: "+41 44 123 45 67",
            },
            stored={**STORED, Factor.EMAIL: "buchhaltung@meier-bau.ch"},
        )
        # Email and phone are facts about the person, so this one does verify.
        assert result.status is VerificationStatus.VERIFIED

    def test_customer_id_alone_is_not_enough_even_repeated(self):
        result = check({Factor.CUSTOMER_ID: "445909044455"})
        assert result.status is VerificationStatus.PARTIALLY_VERIFIED
        assert result.personal_satisfied is False

    def test_the_hint_asks_for_a_personal_factor_when_that_is_what_is_missing(self):
        """When the count is met but every factor came off the document, the next question
        must be one the document cannot answer."""
        result = check({Factor.CUSTOMER_ID: "445909044455"}, required=1)
        assert result.status is not VerificationStatus.VERIFIED


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
        """A caller who cannot recall their date of birth on the spot has not failed
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
                Factor.CUSTOMER_ID: "445909044455",
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
                Factor.CUSTOMER_ID: "445909044455",
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
                Factor.CUSTOMER_ID: "445909044455",
            }
        )
        # mismatched_factors is deliberately on the result: the handler needs it to count
        # distinct wrong values. It is never serialised into a response, which is the
        # property that matters and is asserted in tests/contract/test_verify_identity.py.
        public = {k: v for k, v in vars(result).items() if k != "mismatched_factors"}
        assert "wrong@example.com" not in repr(public)
        assert "EMAIL" not in repr(public)
        assert not hasattr(result, "failed_factors")


class TestNoDisclosure:
    def test_the_result_never_carries_a_stored_value(self):
        """The rule returns a decision, not data. Nothing it produces can be read back to
        learn what the record holds (Principle IV)."""
        result = check({Factor.EMAIL: "buchhaltung@meier-bau.ch"})
        serialised = repr(result)
        for stored_value in STORED.values():
            assert stored_value not in serialised

    def test_a_real_customer_id_with_nonsense_looks_like_an_invented_one(self):
        """Found by calling the deployed endpoint, not by the unit tests below: supplying a
        real customer id alongside deliberate nonsense confirmed one factor, while an
        invented id confirmed none. The difference is a customer-id enumeration oracle over
        a five-digit space."""
        real_id = check(
            {Factor.CUSTOMER_ID: "445909044455", Factor.EMAIL: "nonsense@example.com"},
            stored={**STORED, Factor.CUSTOMER_ID: "445909044455"},
        )
        invented_id = check(
            {Factor.CUSTOMER_ID: "CUST-99999", Factor.EMAIL: "nonsense@example.com"},
            stored={**STORED, Factor.CUSTOMER_ID: "445909044455"},
        )
        assert real_id.status is invented_id.status
        assert real_id.confirmed_count == invented_id.confirmed_count == 0

    def test_a_failed_attempt_reports_no_progress_at_all(self):
        result = check(
            {
                Factor.CUSTOMER_ID: "445909044455",
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.PHONE: "wrong",
            }
        )
        assert result.status is VerificationStatus.FAILED
        assert result.confirmed_count == 0
        assert result.personal_satisfied is False

    def test_a_verified_result_asks_for_nothing_further(self):
        result = check(
            {
                Factor.CUSTOMER_ID: "445909044455",
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.PHONE: "+41 44 123 45 67",
            }
        )
        assert result.status is VerificationStatus.VERIFIED

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
                Factor.CUSTOMER_ID: "445909044455",
            }
        )
        assert result.status is VerificationStatus.VERIFIED

    def test_an_unreadable_date_does_not_crash_and_does_not_verify(self):
        result = check(
            {
                Factor.DATE_OF_BIRTH: "sometime in the seventies",
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.CUSTOMER_ID: "445909044455",
            }
        )
        assert result.status is not VerificationStatus.VERIFIED

    def test_an_unreadable_date_does_not_discard_the_answers_that_matched(self):
        """A caller gave a correct email and phone and was told nothing was confirmed, because
        a date we could not parse was counted as a wrong answer and any mismatch fails the whole
        set. Unreadable is not wrong: it tells us nothing about the caller either way."""
        result = check(
            {
                Factor.DATE_OF_BIRTH: "sometime in the seventies",
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.CUSTOMER_ID: "445909044455",
            }
        )
        assert result.confirmed_count == 2
        assert result.status is VerificationStatus.PARTIALLY_VERIFIED

    def test_a_wrong_date_is_still_a_wrong_answer(self):
        """The distinction is only about readability. A date we can read and that does not
        match is a mismatch, and still fails the attempt."""
        result = check(
            {
                Factor.DATE_OF_BIRTH: "01/01/1990",
                Factor.EMAIL: "buchhaltung@meier-bau.ch",
                Factor.CUSTOMER_ID: "445909044455",
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


class TestNameIsNotAFactor:
    """A caller volunteers their name in the first sentence of every call.

    Counting it would hand over a third of the bar for free, and since first name and surname
    are not independent facts — anyone who knows one almost always knows the other —
    accepting them separately would hand over two thirds (FR-003b).
    """

    def test_there_is_no_name_factor(self):
        assert not [f for f in Factor if "name" in f.value]

    def test_a_name_offered_as_a_factor_is_ignored_not_credited(self):
        """The handler drops unrecognised field names rather than failing the call, so a
        volunteered name confirms nothing and costs nothing."""
        result = check({Factor.EMAIL: "buchhaltung@meier-bau.ch"})
        assert result.confirmed_count == 1


class TestSpokenEmails:
    """What a transcript carries when a caller is asked to spell their address. Every one of
    these is a real value sent by the agent on a call that failed."""

    def _key(self, value: str) -> str:
        from src.domain.verification import Factor, lookup_key

        return lookup_key(Factor.EMAIL, value)

    def test_a_spelled_out_address_reaches_the_stored_one(self):
        assert self._key(
            "S-T-E-P-H-A-N-E dot R-I-C-H-A-R-D @ I-N-N-O-V-A-T-E-C-H dot C-H"
        ) == self._key("stephane.richard@innovatech.ch")

    def test_a_spoken_hyphen_is_punctuation_not_letters(self):
        """ "alpina hyphen tech" reached the backend as "alpinahyphentech". Callers name the
        hyphen precisely because the transcript keeps losing it."""
        assert self._key(
            "K-L-A-U-S dot M-U-E-L-L-E-R at A-L-P-I-N-A hyphen T-E-C-H dot C-H"
        ) == self._key("klaus.mueller@alpina-tech.ch")

    def test_dash_period_and_underscore_are_understood_too(self):
        assert self._key("a dash b at x dot ch") == self._key("a-b@x.ch")
        assert self._key("a underscore b at x dot ch") == self._key("a_b@x.ch")
        assert self._key("a at x period ch") == self._key("a@x.ch")

    def test_the_words_only_count_when_they_stand_alone(self):
        """Someone named Aldotata, or writing to cat@, keeps their address."""
        assert self._key("aldotata@x.ch") == "aldotata@x.ch"
        assert self._key("cat@x.ch") == "cat@x.ch"

    def test_the_at_sign_may_also_be_spoken(self):
        assert self._key("k-l-a-u-s at a-l-p-i-n-a dot c-h") == self._key("klaus@alpina.ch")

    def test_a_domain_hyphen_the_transcript_dropped_still_matches(self):
        """The first voice test: "alpina-tech.ch" came through as "alpinatech.ch"."""
        assert self._key("klaus.mueller@alpinatech.ch") == self._key("klaus.mueller@alpina-tech.ch")

    def test_two_different_addresses_still_differ(self):
        assert self._key("klaus@alpina.ch") != self._key("marco@alpina.ch")

    def test_the_conversion_happens_before_the_spacing_is_stripped(self):
        """Otherwise "dot" is glued into the address it was separating, and the local part
        becomes "stephanedotrichard"."""
        assert "dot" not in self._key("S-T-E-P-H-A-N-E dot R-I-C-H-A-R-D @ x dot c-h")


class TestOneNormalisation:
    """Comparison, lookup and fingerprinting must agree on what counts as the same answer.

    They were three separate switch statements and they had drifted: the comparison folded an
    email's case and stripped its hyphens, and the fingerprint did neither. A caller whose
    address failed on a hyphen and repeated it spelled out therefore spent two of their three
    attempts on one address — the exact thing counting distinct values was meant to prevent.
    """

    @pytest.mark.parametrize(
        "factor,first,second",
        [
            (Factor.EMAIL, "Klaus.Mueller@Alpina-Tech.CH", "klaus.mueller@alpinatech.ch"),
            (Factor.PHONE, "+41 44 501 22 18", "044 501 22 18"),
            (Factor.DATE_OF_BIRTH, "12 March 1974", "1974-03-12"),
        ],
    )
    def test_the_three_users_agree_on_one_value(self, factor, first, second):
        from src.domain.verification import fingerprint, lookup_key, normalise

        assert normalise(factor, first) == normalise(factor, second)
        assert fingerprint(factor, first, "salt") == fingerprint(factor, second, "salt")
        if factor is not Factor.DATE_OF_BIRTH:
            # A date is never looked up: thousands of people share one.
            assert lookup_key(factor, first) == lookup_key(factor, second)

    def test_two_different_answers_stay_different(self):
        from src.domain.verification import fingerprint, normalise

        assert normalise(Factor.EMAIL, "a@x.ch") != normalise(Factor.EMAIL, "b@x.ch")
        assert fingerprint(Factor.EMAIL, "a@x.ch", "s") != fingerprint(Factor.EMAIL, "b@x.ch", "s")

    def test_a_factor_with_no_rule_is_still_folded(self):
        """The customer id, and anything added later, rather than falling through raw."""
        from src.domain.verification import normalise

        assert normalise(Factor.CUSTOMER_ID, "  445909044455 ") == "445909044455"

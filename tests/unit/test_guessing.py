"""Telling a caller correcting themselves apart from a caller trying values.

The distinction is the whole rule. People misspeak, read the wrong line off a document, and
correct themselves — that is an ordinary call. Someone offering a third different customer
number for the same account is working through possibilities, and no single one of those
answers is wrong in a way the earlier rules would catch.

Counted server-side, because an agent that merely notices someone fishing is not a control
(FR-006d).
"""

import pytest

from src.domain.verification import (
    DEFAULT_MAX_DISTINCT_VALUES,
    Factor,
    fingerprint,
    is_enumerating,
)

SALT = "test-salt-not-the-deployed-one"
ALLOWANCE = DEFAULT_MAX_DISTINCT_VALUES


class TestFingerprinting:
    def test_the_same_answer_fingerprints_the_same_way(self):
        first = fingerprint(Factor.EMAIL, "klaus@example.ch", SALT)
        second = fingerprint(Factor.EMAIL, "klaus@example.ch", SALT)
        assert first == second

    def test_a_different_answer_fingerprints_differently(self):
        assert fingerprint(Factor.EMAIL, "a@example.ch", SALT) != fingerprint(
            Factor.EMAIL, "b@example.ch", SALT
        )

    @pytest.mark.parametrize(
        "spoken", ["+41 44 501 22 18", "044 501 22 18", "0041445012218", "44 501 22 18"]
    )
    def test_one_phone_number_said_four_ways_is_one_attempt(self, spoken):
        """Normalised before hashing, so a caller repeating an answer in a different form is
        not counted as trying something new. Without this, an honest caller whose number the
        transcriber renders inconsistently would be locked out for it."""
        canonical = fingerprint(Factor.PHONE, "+41 44 501 22 18", SALT)
        assert fingerprint(Factor.PHONE, spoken, SALT) == canonical

    def test_one_date_written_two_ways_is_one_attempt(self):
        assert fingerprint(Factor.DATE_OF_BIRTH, "12.03.1974", SALT) == fingerprint(
            Factor.DATE_OF_BIRTH, "1974-03-12", SALT
        )

    def test_case_and_spacing_do_not_make_a_new_attempt(self):
        assert fingerprint(Factor.EMAIL, "  Klaus@Example.CH ", SALT) == fingerprint(
            Factor.EMAIL, "klaus@example.ch", SALT
        )

    def test_the_same_string_offered_for_two_fields_counts_separately(self):
        """Otherwise a caller could offer one value as an email and again as a customer id
        and have it counted once."""
        assert fingerprint(Factor.EMAIL, "2019", SALT) != fingerprint(
            Factor.DATE_OF_BIRTH, "2019", SALT
        )

    def test_a_different_salt_produces_a_different_fingerprint(self):
        """Fingerprints are not comparable across deployments, and a stolen table from one
        cannot be matched against another."""
        assert fingerprint(Factor.EMAIL, "klaus@example.ch", SALT) != fingerprint(
            Factor.EMAIL, "klaus@example.ch", "a-different-salt"
        )

    def test_the_answer_cannot_be_read_back_out_of_the_fingerprint(self):
        """It is a keyed digest, not an encoding. This is what lets distinct attempts be
        counted without retaining what was guessed (FR-006c)."""
        value = "1974-03-12"
        printed = fingerprint(Factor.DATE_OF_BIRTH, value, SALT)
        assert value not in printed
        assert "1974" not in printed
        assert len(printed) == 16


class TestOneCorrectionIsAllowed:
    def test_a_single_answer_is_fine(self):
        assert is_enumerating({Factor.CUSTOMER_ID: 1}, ALLOWANCE) is None

    def test_correcting_yourself_once_is_fine(self):
        """The common case: a caller reads the wrong line, then the right one."""
        assert is_enumerating({Factor.CUSTOMER_ID: 2}, ALLOWANCE) is None

    def test_a_third_distinct_value_is_enumeration(self):
        assert is_enumerating({Factor.CUSTOMER_ID: 3}, ALLOWANCE) is Factor.CUSTOMER_ID

    def test_it_triggers_regardless_of_whether_any_value_was_correct(self):
        """A caller who guesses the right customer id on the third attempt has still
        enumerated. The rule is about the behaviour, not the outcome."""
        assert is_enumerating({Factor.EMAIL: 4}, ALLOWANCE) is Factor.EMAIL


class TestTheAllowanceIsPerField:
    def test_correcting_an_email_does_not_spend_the_customer_id_allowance(self):
        """An honest caller with an unusual surname already has more to correct than most,
        and should not be treated as an attacker for it (FR-006b)."""
        assert is_enumerating({Factor.EMAIL: 2, Factor.CUSTOMER_ID: 2}, ALLOWANCE) is None

    def test_totals_across_fields_do_not_accumulate(self):
        counts = {Factor.EMAIL: 2, Factor.PHONE: 2, Factor.DATE_OF_BIRTH: 2}
        assert is_enumerating(counts, ALLOWANCE) is None

    def test_one_field_over_the_line_is_enough(self):
        counts = {Factor.EMAIL: 1, Factor.CUSTOMER_ID: 3}
        assert is_enumerating(counts, ALLOWANCE) is Factor.CUSTOMER_ID

    def test_the_offending_field_is_named_so_the_signal_can_say_which(self):
        assert is_enumerating({Factor.PHONE: 5}, ALLOWANCE) is Factor.PHONE


class TestConfigurability:
    def test_the_allowance_is_a_parameter(self):
        """Principle VIII. A stricter deployment sets it to one and allows no correction at
        all; a demo could set it higher."""
        assert is_enumerating({Factor.CUSTOMER_ID: 2}, 1) is Factor.CUSTOMER_ID
        assert is_enumerating({Factor.CUSTOMER_ID: 3}, 5) is None

    def test_nothing_offered_is_never_enumeration(self):
        assert is_enumerating({}, ALLOWANCE) is None


class TestOneImplementation:
    """It was written twice, once in each endpoint that takes an answer, and the copies had
    begun to differ in what they recorded. A rule that holds on one path and not the other is
    not a rule."""

    def test_both_endpoints_call_the_same_function(self):
        import inspect

        from src.handlers import check_factor, verify_identity

        for module in (check_factor, verify_identity):
            source = inspect.getsource(module)
            assert "guessing.record_and_check" in source
            # No local copy left behind to drift again.
            assert "is_enumerating(" not in source

    def test_it_records_before_it_decides(self):
        """A caller must not learn from the attempt that stops them whether it was right, so
        the value is counted before anything is compared."""
        import inspect

        from src.common import guessing

        source = inspect.getsource(guessing.record_and_check)
        assert source.index("record_factor_attempts") < source.index("is_enumerating")

"""Payment matching rules (FR-010a, FR-010b, FR-010d).

These tests are the executable form of the spec's matching rule, written before the
implementation so the rule is fixed by the requirement rather than by whatever the code
happens to do. The rule decides whether a caller disputing an overdue invoice is told
their payment was found, so getting it wrong is either a customer told their correct
payment does not exist, or a stranger told about someone else's.
"""

from datetime import date
from decimal import Decimal

import pytest
from src.domain.payment_match import (
    MatchStatus,
    PaymentCandidate,
    ReferenceLink,
    match_payment,
)

INVOICE_AMOUNT = Decimal("4200.00")
RECORDED_DATE = date(2026, 7, 6)  # a Monday: the date the payment arrived
TOLERANCE_DAYS = 3
TYPO_DISTANCE = 2

INVOICE_NUMBER = "INV-2026-0412"  # sequential, human-readable, spoken to the caller
PAYMENT_REFERENCE = "1467900"  # random 7-digit key, quoted on the transfer

# What resolves to a real invoice. The next invoice in sequence exists, which is the
# situation that makes fuzzy matching on invoice_number unsafe (FR-010h).
KNOWN_REFERENCES = frozenset({INVOICE_NUMBER, PAYMENT_REFERENCE, "INV-2026-0413", "8830142"})


def candidate(
    entry_id: str = "pay_001",
    amount: str = "4200.00",
    execution_date: date = RECORDED_DATE,
    status: str = "UNALLOCATED",
    reference: str | None = None,
    payer_name_matches: bool = True,
    payer_address_matches: bool = True,
) -> PaymentCandidate:
    """Builds one payment record. Defaults describe the golden-path fixture: an unallocated
    payment carrying no reference at all."""
    return PaymentCandidate(
        entry_id=entry_id,
        amount=Decimal(amount),
        execution_date=execution_date,
        status=status,
        reference=reference,
        payer_name_matches=payer_name_matches,
        payer_address_matches=payer_address_matches,
    )


def match(claimed_amount, claimed_date, candidates=None, tolerance=TOLERANCE_DAYS):
    """Calls the rule with the golden-path invoice and a single default candidate."""
    return match_payment(
        claimed_amount=claimed_amount,
        claimed_transfer_date=claimed_date,
        invoice_amount=INVOICE_AMOUNT,
        invoice_number=INVOICE_NUMBER,
        invoice_payment_reference=PAYMENT_REFERENCE,
        candidates=[candidate()] if candidates is None else candidates,
        known_references=KNOWN_REFERENCES,
        tolerance_days=tolerance,
        typo_distance=TYPO_DISTANCE,
    )


class TestMatch:
    def test_exact_amount_and_date_matches(self):
        result = match(Decimal("4200.00"), RECORDED_DATE)
        assert result.status is MatchStatus.MATCH
        assert result.payment_entry_id == "pay_001"
        assert result.covers_invoice is True

    def test_transfer_sent_friday_matches_payment_recorded_monday(self):
        """The tolerance exists for exactly this case: the payer sees the date the transfer
        left, the record holds the date it arrived."""
        friday = date(2026, 7, 3)
        assert (RECORDED_DATE - friday).days == TOLERANCE_DAYS
        assert match(Decimal("4200.00"), friday).status is MatchStatus.MATCH

    @pytest.mark.parametrize("days_earlier", [0, 1, 2, 3])
    def test_every_day_inside_the_tolerance_matches(self, days_earlier):
        claimed = date(2026, 7, 6 - days_earlier)
        assert match(Decimal("4200.00"), claimed).status is MatchStatus.MATCH

    def test_payment_larger_than_the_invoice_still_matches(self):
        """A surplus payment is a real payment. Refusing to match it would strand the money;
        the excess is the reviewing human's problem, not the caller's."""
        result = match(
            Decimal("5000.00"),
            RECORDED_DATE,
            candidates=[candidate(amount="5000.00")],
        )
        assert result.status is MatchStatus.MATCH
        assert result.covers_invoice is True

    def test_payment_smaller_than_the_invoice_matches_but_does_not_cover(self):
        result = match(
            Decimal("1000.00"),
            RECORDED_DATE,
            candidates=[candidate(amount="1000.00")],
        )
        assert result.status is MatchStatus.MATCH
        assert result.covers_invoice is False


class TestNoMatch:
    def test_one_franc_out_does_not_match(self):
        """The amount carries no tolerance at all. It is the field that stops a caller
        holding the invoice from guessing their way to a match."""
        assert match(Decimal("4201.00"), RECORDED_DATE).status is MatchStatus.NO_MATCH

    def test_one_centime_out_does_not_match(self):
        assert match(Decimal("4200.01"), RECORDED_DATE).status is MatchStatus.NO_MATCH

    def test_a_day_beyond_the_tolerance_does_not_match(self):
        four_days_earlier = date(2026, 7, 2)
        assert match(Decimal("4200.00"), four_days_earlier).status is MatchStatus.NO_MATCH

    def test_a_date_after_the_record_does_not_match(self):
        """The tolerance is backward-only. A payment cannot post before it was sent, so a
        later claimed date is a genuine mismatch, not a banking artefact."""
        day_after = date(2026, 7, 7)
        assert match(Decimal("4200.00"), day_after).status is MatchStatus.NO_MATCH

    def test_no_candidates_does_not_match(self):
        assert (
            match(Decimal("4200.00"), RECORDED_DATE, candidates=[]).status is MatchStatus.NO_MATCH
        )

    def test_an_unsettled_payment_never_matches(self):
        """FR-010d. A pending payment is not money that has arrived, and reporting it as a
        match would tell the caller their invoice is covered when it may yet fail."""
        result = match(
            Decimal("4200.00"),
            RECORDED_DATE,
            candidates=[candidate(status="PENDING")],
        )
        assert result.status is not MatchStatus.MATCH


class TestInsufficient:
    def test_neither_field_supplied(self):
        result = match(None, None)
        assert result.status is MatchStatus.INSUFFICIENT
        assert set(result.missing_fields) == {"claimed_amount", "claimed_transfer_date"}

    def test_amount_without_a_date(self):
        result = match(Decimal("4200.00"), None)
        assert result.status is MatchStatus.INSUFFICIENT
        assert result.missing_fields == ("claimed_transfer_date",)

    def test_date_without_an_amount(self):
        result = match(None, RECORDED_DATE)
        assert result.status is MatchStatus.INSUFFICIENT
        assert result.missing_fields == ("claimed_amount",)

    def test_two_candidates_fitting_the_same_details_is_ambiguous(self):
        """Two identical payments must not be resolved by picking one. Guessing would
        allocate the wrong payment and look like it worked."""
        result = match(
            Decimal("4200.00"),
            RECORDED_DATE,
            candidates=[candidate("pay_001"), candidate("pay_002")],
        )
        assert result.status is MatchStatus.INSUFFICIENT
        assert result.payment_entry_id is None

    def test_ambiguity_is_only_among_candidates_that_actually_fit(self):
        """A second payment that does not fit the claimed details is not ambiguity."""
        result = match(
            Decimal("4200.00"),
            RECORDED_DATE,
            candidates=[candidate("pay_001"), candidate("pay_002", amount="99.00")],
        )
        assert result.status is MatchStatus.MATCH
        assert result.payment_entry_id == "pay_001"


class TestReference:
    """The reference is the allocation key. It is read from the record, never asked of the
    caller (FR-010i), and it is what explains why a correct payment went unallocated."""

    def test_absent_reference_is_the_golden_path(self):
        result = match(Decimal("4200.00"), RECORDED_DATE)
        assert result.status is MatchStatus.MATCH
        assert result.reference_link is ReferenceLink.ABSENT

    def test_exact_payment_reference(self):
        result = match(
            Decimal("4200.00"),
            RECORDED_DATE,
            candidates=[candidate(reference=PAYMENT_REFERENCE)],
        )
        assert result.status is MatchStatus.MATCH
        assert result.reference_link is ReferenceLink.EXACT

    def test_the_invoice_number_is_also_accepted_as_exact(self):
        """Customers write the invoice number instead of the reference often enough that
        refusing it would be pedantry. It is unambiguous either way."""
        result = match(
            Decimal("4200.00"),
            RECORDED_DATE,
            candidates=[candidate(reference=INVOICE_NUMBER)],
        )
        assert result.reference_link is ReferenceLink.EXACT

    @pytest.mark.parametrize(
        "mistyped",
        [
            "1467901",  # last digit wrong
            "146790",  # a digit dropped
            "1469700",  # two digits transposed
            "1467090",  # two digits transposed further in
        ],
    )
    def test_a_mistyped_payment_reference_that_resolves_to_nothing_is_a_typo(self, mistyped):
        """The common real failure: a correct payment that never allocated because someone
        mistyped the key. Safe to infer only because the keyspace is sparse."""
        result = match(
            Decimal("4200.00"),
            RECORDED_DATE,
            candidates=[candidate(reference=mistyped)],
        )
        assert result.status is MatchStatus.MATCH
        assert result.reference_link is ReferenceLink.PROBABLE_TYPO

    def test_a_reference_resolving_to_another_invoice_blocks_the_match(self):
        """8830142 is a real key belonging to another invoice. The amount and date agree, so
        without this rule the payment would be proposed against the wrong invoice."""
        result = match(
            Decimal("4200.00"),
            RECORDED_DATE,
            candidates=[candidate(reference="8830142")],
        )
        assert result.status is MatchStatus.NO_MATCH
        assert result.reference_link is ReferenceLink.OTHER_INVOICE

    def test_the_next_invoice_number_is_not_treated_as_a_typo_of_this_one(self):
        """This is the case the two-identifier design exists for. INV-2026-0413 is one edit
        from INV-2026-0412 and is a real invoice, so the payment belongs there. A rule that
        fuzzy-matched invoice numbers would allocate it here instead (FR-010h)."""
        result = match(
            Decimal("4200.00"),
            RECORDED_DATE,
            candidates=[candidate(reference="INV-2026-0413")],
        )
        assert result.status is MatchStatus.NO_MATCH
        assert result.reference_link is ReferenceLink.OTHER_INVOICE

    def test_an_unrelated_reference_neither_blocks_nor_explains(self):
        result = match(
            Decimal("4200.00"),
            RECORDED_DATE,
            candidates=[candidate(reference="Rechnung Mai")],
        )
        assert result.status is MatchStatus.MATCH
        assert result.reference_link is ReferenceLink.UNRELATED


class TestPayerIdentity:
    """A payment may legitimately come from a parent company, a group entity, or a third
    party acting for the customer. Neither name nor address may refuse a match (FR-010j)."""

    def test_a_different_payer_name_still_matches(self):
        result = match(
            Decimal("4200.00"),
            RECORDED_DATE,
            candidates=[candidate(payer_name_matches=False)],
        )
        assert result.status is MatchStatus.MATCH
        assert result.payer_name_discrepancy is True

    def test_a_different_payer_name_and_address_still_matches(self):
        result = match(
            Decimal("4200.00"),
            RECORDED_DATE,
            candidates=[candidate(payer_name_matches=False, payer_address_matches=False)],
        )
        assert result.status is MatchStatus.MATCH
        assert result.payer_name_discrepancy is True
        assert result.address_discrepancy is True


class TestDisclosure:
    def test_the_address_discrepancy_is_a_flag_and_never_the_address(self):
        """FR-010. The rule is given a boolean, so there is no stored address for it to
        return even by mistake."""
        result = match(
            Decimal("4200.00"),
            RECORDED_DATE,
            candidates=[candidate(payer_address_matches=False)],
        )
        assert result.status is MatchStatus.MATCH
        assert result.address_discrepancy is True

    def test_a_failed_match_reveals_nothing_about_the_record(self):
        """A NO_MATCH result must not leak which payment nearly fitted, or the caller could
        probe amounts until the response changed."""
        result = match(Decimal("9999.00"), RECORDED_DATE)
        assert result.payment_entry_id is None
        assert result.covers_invoice is False
        assert result.address_discrepancy is False
        assert result.payer_name_discrepancy is False

    def test_the_rule_is_never_given_a_name_or_an_address(self):
        """PaymentCandidate carries booleans, not values. The rule cannot leak a payer name
        or address because it never receives one (FR-010)."""
        fields = PaymentCandidate.__dataclass_fields__
        assert fields["payer_name_matches"].type in (bool, "bool")
        assert fields["payer_address_matches"].type in (bool, "bool")

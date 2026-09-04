"""What a credit must be attached to before it can be granted (FR-013, FR-013a).

Separate from the ceilings, because these are questions about whether the request makes
sense at all rather than about how much. A credit with no identified charge cannot be
reconciled by anyone afterwards, and one against a charge already under review would
pre-empt the person reviewing it.

Also covers the rolling window itself: which credits count toward the twelve-month total,
and which have aged out.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.domain.credit import CreditableEntry, entry_from_ledger, rolling_credit_total

TODAY = date(2026, 9, 3)
WINDOW_MONTHS = 12


def credit(amount: str, days_ago: int) -> dict:
    """A credit note as the ledger stores it: negative, because it reduces what is owed."""
    return {
        "type": "CREDIT_NOTE",
        "amount": Decimal(f"-{amount}"),
        "entry_date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "status": "APPROVED",
    }


class TestTheRollingWindow:
    def test_credits_inside_the_window_are_summed(self):
        entries = [credit("90.00", 300), credit("95.00", 210), credit("100.00", 150)]
        assert rolling_credit_total(entries, TODAY, WINDOW_MONTHS) == Decimal("285.00")

    def test_a_credit_older_than_the_window_has_aged_out(self):
        entries = [credit("90.00", 400), credit("50.00", 30)]
        assert rolling_credit_total(entries, TODAY, WINDOW_MONTHS) == Decimal("50.00")

    def test_the_boundary_day_still_counts(self):
        """A rule that quietly drops a credit on its 365th day is a rule nobody can audit."""
        entries = [credit("90.00", 365)]
        assert rolling_credit_total(entries, TODAY, WINDOW_MONTHS) == Decimal("90.00")

    def test_a_day_past_the_boundary_does_not(self):
        assert rolling_credit_total([credit("90.00", 366)], TODAY, WINDOW_MONTHS) == Decimal("0")

    def test_only_credit_notes_count(self):
        """Invoices and payments are not goodwill, however they are signed."""
        entries = [
            credit("50.00", 30),
            {"type": "PAYMENT", "amount": Decimal("-4200.00"), "entry_date": TODAY.isoformat()},
            {"type": "INVOICE", "amount": Decimal("4200.00"), "entry_date": TODAY.isoformat()},
        ]
        assert rolling_credit_total(entries, TODAY, WINDOW_MONTHS) == Decimal("50.00")

    def test_a_rejected_credit_does_not_count_against_the_customer(self):
        """One that was asked for and refused was never given."""
        entries = [credit("50.00", 30), {**credit("100.00", 20), "status": "REJECTED"}]
        assert rolling_credit_total(entries, TODAY, WINDOW_MONTHS) == Decimal("50.00")

    def test_a_credit_still_awaiting_approval_does_count(self):
        """It is committed even if not yet confirmed, and counting it later would let a
        caller stack requests faster than they can be approved."""
        entries = [{**credit("100.00", 5), "status": "PENDING_APPROVAL"}]
        assert rolling_credit_total(entries, TODAY, WINDOW_MONTHS) == Decimal("100.00")

    def test_no_history_is_zero_not_an_error(self):
        assert rolling_credit_total([], TODAY, WINDOW_MONTHS) == Decimal("0")

    def test_the_window_length_is_a_parameter(self):
        """Principle VIII: a stricter deployment shortens it, a laxer one extends it."""
        entries = [credit("90.00", 200)]
        assert rolling_credit_total(entries, TODAY, 12) == Decimal("90.00")
        assert rolling_credit_total(entries, TODAY, 3) == Decimal("0")


class TestBuildingTheChargeFromTheLedger:
    def test_an_invoice_becomes_a_creditable_charge(self):
        entry = entry_from_ledger(
            {
                "entry_id": "inv_1",
                "type": "INVOICE",
                "amount": Decimal("4200.00"),
                "status": "OPEN",
            },
            existing_credits=[],
        )
        assert entry.amount == Decimal("4200.00")
        assert entry.already_credited == Decimal("0")

    def test_credits_already_applied_to_it_are_totalled(self):
        entry = entry_from_ledger(
            {"entry_id": "inv_1", "type": "INVOICE", "amount": Decimal("50.00"), "status": "OPEN"},
            existing_credits=[
                {"amount": Decimal("-20.00"), "allocated_to": ["inv_1"], "status": "APPROVED"},
                {"amount": Decimal("-10.00"), "allocated_to": ["inv_1"], "status": "APPROVED"},
            ],
        )
        assert entry.already_credited == Decimal("30.00")

    def test_credits_against_other_charges_are_not_counted(self):
        entry = entry_from_ledger(
            {"entry_id": "inv_1", "type": "INVOICE", "amount": Decimal("50.00"), "status": "OPEN"},
            existing_credits=[
                {"amount": Decimal("-20.00"), "allocated_to": ["inv_2"], "status": "APPROVED"}
            ],
        )
        assert entry.already_credited == Decimal("0")

    def test_a_charge_under_review_carries_its_dispute(self):
        entry = entry_from_ledger(
            {
                "entry_id": "inv_1",
                "type": "INVOICE",
                "amount": Decimal("50.00"),
                "status": "UNDER_REVIEW",
            },
            existing_credits=[],
        )
        assert entry.has_open_dispute is True

    @pytest.mark.parametrize("wrong_type", ["PAYMENT", "CREDIT_NOTE", "ADJUSTMENT"])
    def test_only_a_charge_can_be_credited(self, wrong_type):
        """Crediting a payment or another credit is not a thing that means anything."""
        assert (
            entry_from_ledger(
                {"entry_id": "x", "type": wrong_type, "amount": Decimal("50.00"), "status": "OPEN"},
                existing_credits=[],
            )
            is None
        )

    def test_a_missing_entry_yields_nothing_rather_than_raising(self):
        """The agent may name a charge that does not exist. That is a refusal, not a crash."""
        assert entry_from_ledger(None, existing_credits=[]) is None


class TestTheChargeContract:
    def test_remaining_is_what_is_left_to_credit(self):
        charge = CreditableEntry(
            entry_id="inv_1",
            amount=Decimal("50.00"),
            already_credited=Decimal("30.00"),
            has_open_dispute=False,
        )
        assert charge.remaining == Decimal("20.00")

    def test_a_fully_credited_charge_has_nothing_left(self):
        charge = CreditableEntry(
            entry_id="inv_1",
            amount=Decimal("50.00"),
            already_credited=Decimal("50.00"),
            has_open_dispute=False,
        )
        assert charge.remaining == Decimal("0")

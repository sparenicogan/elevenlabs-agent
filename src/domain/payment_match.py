"""Deciding whether a caller's claimed payment is the one on record.

This is the rule the golden path turns on. A caller disputing an overdue invoice states
what they paid and when; the backend compares that against the payments it holds and says
MATCH, NO_MATCH, or INSUFFICIENT. Getting it wrong means either telling a customer their
correct payment does not exist, or telling a stranger about someone else's.

Pure by design: no database, no clock, no network. The handler performs the lookups and
passes candidates in, which is what lets the rule be tested exhaustively without AWS and
what makes Constitution Principle II checkable rather than aspirational.
"""

from collections.abc import Container
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

# A payment must actually have arrived before it can be reported as covering anything.
# A pending payment may yet fail, and saying otherwise would be inventing state (FR-010d).
SETTLED_STATUSES = frozenset({"UNALLOCATED", "UNDER_REVIEW", "ALLOCATED"})


class MatchStatus(StrEnum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    INSUFFICIENT = "INSUFFICIENT"


class ReferenceLink(StrEnum):
    """How the payment's written reference relates to the invoice under discussion."""

    EXACT = "EXACT"
    PROBABLE_TYPO = "PROBABLE_TYPO"
    OTHER_INVOICE = "OTHER_INVOICE"
    ABSENT = "ABSENT"
    UNRELATED = "UNRELATED"


@dataclass(frozen=True)
class PaymentCandidate:
    """
    One payment held against the customer, reduced to what the rule may see.

    entry_id:              ledger entry identifier.
    amount:                positive magnitude of the payment, in the invoice's currency.
    execution_date:        the date the payment arrived, as recorded.
    status:                ledger status; only those in SETTLED_STATUSES can match.
    reference:             whatever the payer actually wrote on the transfer, or None.
    payer_name_matches:    whether the payer's name matches the customer on file.
    payer_address_matches: whether the payer's address matches the customer on file.

    The last two are booleans rather than values deliberately: the rule cannot leak a payer
    name or address because it is never given one (FR-010).
    """

    entry_id: str
    amount: Decimal
    execution_date: date
    status: str
    reference: str | None = None
    payer_name_matches: bool = True
    payer_address_matches: bool = True


@dataclass(frozen=True)
class MatchResult:
    """
    The outcome, in the form the tool endpoint returns.

    status:                  MATCH, NO_MATCH or INSUFFICIENT.
    payment_entry_id:        the matched payment, on MATCH only.
    covers_invoice:          whether the payment is at least the invoice amount.
    reference_link:          how the written reference relates to this invoice.
    address_discrepancy:     payer address differs from the customer on file.
    payer_name_discrepancy:  payer name differs from the customer on file.
    missing_fields:          which claimed fields the caller could not supply, on
                             INSUFFICIENT only.
    """

    status: MatchStatus
    payment_entry_id: str | None = None
    covers_invoice: bool = False
    reference_link: ReferenceLink = ReferenceLink.ABSENT
    address_discrepancy: bool = False
    payer_name_discrepancy: bool = False
    missing_fields: tuple[str, ...] = ()


def edit_distance(left: str, right: str) -> int:
    """
    Counts single-character insertions, deletions and substitutions between two strings.

    left, right: the strings to compare.

    Returns: the Levenshtein distance. A transposition costs 2 here rather than 1, which is
             deliberate: it still falls inside the default tolerance, and the simpler
             algorithm has no edge cases to get wrong.
    """
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (left_char != right_char),  # substitution
                )
            )
        previous = current
    return previous[-1]


def classify_reference(
    reference: str | None,
    invoice_number: str,
    invoice_payment_reference: str,
    known_references: Container[str],
    typo_distance: int,
) -> ReferenceLink:
    """
    Works out what a payment's written reference says about which invoice it belongs to.

    reference:                 what the payer wrote, or None.
    invoice_number:            this invoice's human-readable id, e.g. INV-2026-0412.
    invoice_payment_reference: this invoice's random 7-digit key.
    known_references:          anything that resolves to a real invoice, across customers.
    typo_distance:             how many edits still count as a mistyped key.

    Returns: the classification. Order matters: a reference that resolves to another invoice
             is checked before near-misses, because a mistyped key that happens to land on a
             real invoice belongs to that invoice, not to this one (FR-010g).
    """
    if reference is None or not reference.strip():
        return ReferenceLink.ABSENT

    written = reference.strip()

    if written in (invoice_payment_reference, invoice_number):
        return ReferenceLink.EXACT

    # Checked before the typo rule, and that ordering is load-bearing.
    if written in known_references:
        return ReferenceLink.OTHER_INVOICE

    # Fuzzy matching applies only to the random key, never to the sequential invoice number:
    # consecutive invoice numbers differ by one character, so a near miss there is far more
    # likely to be the neighbouring invoice than a typo of this one (FR-010h).
    if edit_distance(written, invoice_payment_reference) <= typo_distance:
        return ReferenceLink.PROBABLE_TYPO

    return ReferenceLink.UNRELATED


def match_payment(
    claimed_amount: Decimal | None,
    claimed_transfer_date: date | None,
    invoice_amount: Decimal,
    invoice_number: str,
    invoice_payment_reference: str,
    candidates: list[PaymentCandidate],
    known_references: Container[str],
    tolerance_days: int,
    typo_distance: int,
) -> MatchResult:
    """
    Decides whether the caller's claimed payment is one of the payments on record.

    claimed_amount:            what the caller says they transferred. None if they cannot say.
    claimed_transfer_date:     the date the caller says they sent it. None if they cannot say.
    invoice_amount:            the invoice under discussion, for the covers_invoice flag.
    invoice_number:            that invoice's human-readable id.
    invoice_payment_reference: that invoice's random 7-digit key.
    candidates:                the customer's payments that could plausibly apply.
    known_references:          anything resolving to a real invoice, across all customers.
    tolerance_days:            how many days earlier than the record the caller's date may be.
    typo_distance:             edit distance still counted as a mistyped reference.

    Returns: a MatchResult. The caller's amount and date must fit before anything at all is
             revealed about a payment, so someone who has not demonstrated knowledge of the
             payment learns nothing from a failed attempt.
    """
    missing = tuple(
        field
        for field, value in (
            ("claimed_amount", claimed_amount),
            ("claimed_transfer_date", claimed_transfer_date),
        )
        if value is None
    )
    if missing:
        return MatchResult(status=MatchStatus.INSUFFICIENT, missing_fields=missing)

    fitting = [
        candidate
        for candidate in candidates
        if candidate.status in SETTLED_STATUSES
        and candidate.amount == claimed_amount
        and 0 <= (candidate.execution_date - claimed_transfer_date).days <= tolerance_days
    ]

    if not fitting:
        return MatchResult(status=MatchStatus.NO_MATCH)

    # Two payments fitting the same details is not something to resolve by choosing one.
    # Guessing would allocate the wrong payment and look like it worked.
    if len(fitting) > 1:
        return MatchResult(status=MatchStatus.INSUFFICIENT)

    payment = fitting[0]
    link = classify_reference(
        payment.reference,
        invoice_number,
        invoice_payment_reference,
        known_references,
        typo_distance,
    )

    # The payment fits, but it is earmarked for a different invoice. Proposing it here would
    # take money from another invoice with the amount and date appearing to agree.
    if link is ReferenceLink.OTHER_INVOICE:
        return MatchResult(status=MatchStatus.NO_MATCH, reference_link=link)

    return MatchResult(
        status=MatchStatus.MATCH,
        payment_entry_id=payment.entry_id,
        covers_invoice=payment.amount >= invoice_amount,
        reference_link=link,
        address_discrepancy=not payment.payer_address_matches,
        payer_name_discrepancy=not payment.payer_name_matches,
    )

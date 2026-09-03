"""Deciding whether a caller has proved who they are.

Nothing financial is disclosed before this returns VERIFIED (FR-001), which makes it the
most consequential rule in the system in both directions: too strict and a real customer is
refused their own invoice, too loose and a stranger hears about someone else's.

Pure by design. This function is given the caller's answers and the stored record and
returns a decision. Locking the account and writing risk signals are effects, and belong to
the handler.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

# The number of trailing digits compared when checking a phone number. Swiss subscriber
# numbers are nine digits after the country code, so comparing the last nine makes
# +41 44 123 45 67, 044 123 45 67 and 0041441234567 the same number — which they are.
PHONE_SIGNIFICANT_DIGITS = 9

# Date formats a spoken date might be transcribed into. ISO first because that is what the
# record holds and what the agent is instructed to produce.
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y")


class Factor(StrEnum):
    # A name is deliberately absent. Callers volunteer it in the first sentence of every
    # call, so counting it would hand over a third of the bar for free — and first name and
    # surname are not independent facts, so accepting them separately would hand over two
    # thirds (FR-003b).
    EMAIL = "email"
    PHONE = "phone"
    DATE_OF_BIRTH = "date_of_birth"
    ACCOUNT_OPENING_YEAR = "account_opening_year"
    CUSTOMER_ID = "customer_id"


# Factors a caller holding the invoice cannot read off it. At least one confirmed factor
# must come from this set, or possession of a document becomes possession of the account
# (FR-003a). The customer id is deliberately excluded: it is printed on the invoice.
NON_DOCUMENT_FACTORS = frozenset(
    {
        Factor.EMAIL,
        Factor.PHONE,
        Factor.DATE_OF_BIRTH,
        Factor.ACCOUNT_OPENING_YEAR,
    }
)


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    FAILED = "FAILED"
    LOCKED = "LOCKED"


@dataclass(frozen=True)
class VerificationResult:
    """
    The decision, carrying no stored value and no indication of which answer was wrong.

    status:                 the outcome the agent must honour.
    confirmed_count:        how many factors were answered correctly.
    required_count:         how many are needed, from policy.
    non_document_satisfied: whether at least one confirmed factor was not readable off an
                            invoice.
    next_factor_hint:       which field to ask for next. A field name, never a value
                            (FR-004).
    is_failed_attempt:      whether this attempt counts toward the lockout. Answering too
                            few questions does not; answering one wrongly does.
    """

    status: VerificationStatus
    confirmed_count: int
    required_count: int
    non_document_satisfied: bool
    next_factor_hint: Factor | None
    is_failed_attempt: bool


def _normalise_phone(value: str) -> str:
    """Reduces a phone number to its significant trailing digits, so written form and
    country-code prefix stop mattering."""
    digits = re.sub(r"\D", "", value)
    return digits[-PHONE_SIGNIFICANT_DIGITS:]


def _normalise_date(value: str) -> str:
    """Parses a spoken-then-transcribed date into ISO form, or returns a sentinel that can
    never equal a stored date so an unparseable answer is simply wrong, not an exception."""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return "\x00unparseable"


def _matches(factor: Factor, supplied: str, stored: str) -> bool:
    """
    Compares one answer against the record, allowing for how it was spoken.

    factor:   which field is being checked.
    supplied: what the caller said, as transcribed.
    stored:   the recorded value.

    Returns: True when they are the same value. Comparison is normalised per factor: a
             caller does not speak capital letters, and the same phone number has several
             written forms.
    """
    if factor is Factor.PHONE:
        return _normalise_phone(supplied) == _normalise_phone(stored)
    if factor is Factor.DATE_OF_BIRTH:
        return _normalise_date(supplied) == _normalise_date(stored)
    return supplied.strip().casefold() == stored.strip().casefold()


def check_factors(
    supplied: dict[Factor, str],
    stored: dict[Factor, str],
    required_count: int,
) -> VerificationResult:
    """
    Decides whether the caller has proved their identity.

    supplied:       the answers given so far in this call, keyed by factor.
    stored:         the identity record. Empty when no customer matched, which must be
                    indistinguishable from wrong answers.
    required_count: how many correct factors are needed, from policy (FR-003).

    Returns: a VerificationResult. VERIFIED needs enough correct factors and at least one
             that is not printed on an invoice. Any wrong answer fails the attempt outright,
             so a caller cannot guess freely by also supplying fields they do know.
    """
    confirmed: set[Factor] = set()
    any_wrong = False

    for factor, value in supplied.items():
        # An unknown customer has no stored values, so every answer is wrong — which is
        # exactly how a wrong answer against a known customer looks. The gate must not
        # reveal whether a customer exists.
        if factor in stored and _matches(factor, value, stored[factor]):
            confirmed.add(factor)
        else:
            any_wrong = True

    non_document_satisfied = bool(confirmed & NON_DOCUMENT_FACTORS)
    enough = len(confirmed) >= required_count

    if any_wrong:
        status = VerificationStatus.FAILED
    elif enough and non_document_satisfied:
        status = VerificationStatus.VERIFIED
    else:
        status = VerificationStatus.PARTIALLY_VERIFIED

    # A failed attempt reports nothing about what was right. Otherwise supplying a real
    # customer id alongside deliberate nonsense returns a higher confirmed count than
    # supplying an invented one, and the count becomes a way to enumerate customer ids.
    if status is VerificationStatus.FAILED:
        return VerificationResult(
            status=status,
            confirmed_count=0,
            required_count=required_count,
            non_document_satisfied=False,
            next_factor_hint=_next_hint(set(), non_document_satisfied=False),
            is_failed_attempt=True,
        )

    return VerificationResult(
        status=status,
        confirmed_count=len(confirmed),
        required_count=required_count,
        non_document_satisfied=non_document_satisfied,
        # Nothing more to ask once verification has succeeded.
        next_factor_hint=(
            None
            if status is VerificationStatus.VERIFIED
            else _next_hint(confirmed, non_document_satisfied)
        ),
        is_failed_attempt=False,
    )


def _next_hint(confirmed: set[Factor], non_document_satisfied: bool) -> Factor | None:
    """
    Chooses which field to ask for next.

    confirmed:              factors already answered correctly.
    non_document_satisfied: whether a non-document factor is among them.

    Returns: a field name, never a value. When the caller has only produced things readable
             off an invoice, the next question is deliberately one the invoice cannot
             answer.
    """
    pool = NON_DOCUMENT_FACTORS if not non_document_satisfied else set(Factor)
    remaining = sorted(pool - confirmed)
    return remaining[0] if remaining else None

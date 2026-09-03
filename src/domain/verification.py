"""Deciding whether a caller has proved who they are.

Nothing financial is disclosed before this returns VERIFIED (FR-001), which makes it the
most consequential rule in the system in both directions: too strict and a real customer is
refused their own invoice, too loose and a stranger hears about someone else's.

Pure by design. This function is given the caller's answers and the stored record and
returns a decision. Locking the account and writing risk signals are effects, and belong to
the handler.
"""

import hashlib
import hmac
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


# The order factors are suggested in, most answerable first. Ordering matters because a
# caller asked for something they cannot produce says so, and the next suggestion is all
# they have to work with — leading with the account opening year, which almost nobody
# remembers, wastes the exchange and makes the gate feel like an obstacle rather than a
# formality. Email and phone are the two most people can give without looking anything up.
ASK_ORDER = (
    Factor.EMAIL,
    Factor.PHONE,
    Factor.DATE_OF_BIRTH,
    Factor.ACCOUNT_OPENING_YEAR,
    Factor.CUSTOMER_ID,
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
    mismatched_factors:     which fields did not match.

    The last field is internal. It exists so the handler can count *distinct wrong values*
    rather than wrong calls — without it, a caller who mistypes one answer has it resent on
    every turn and exhausts the lockout in three, however correct everything else is. It is
    never serialised into a response; the tool body is built field by field, and telling a
    caller which answer failed would turn verification into an oracle (FR-004).
    """

    status: VerificationStatus
    confirmed_count: int
    required_count: int
    non_document_satisfied: bool
    next_factor_hint: Factor | None
    is_failed_attempt: bool
    mismatched_factors: frozenset[Factor] = frozenset()


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
    mismatched: set[Factor] = set()

    for factor, value in supplied.items():
        # An unknown customer has no stored values, so every answer is wrong — which is
        # exactly how a wrong answer against a known customer looks. The gate must not
        # reveal whether a customer exists.
        if factor in stored and _matches(factor, value, stored[factor]):
            confirmed.add(factor)
        else:
            mismatched.add(factor)

    non_document_satisfied = bool(confirmed & NON_DOCUMENT_FACTORS)
    enough = len(confirmed) >= required_count

    if mismatched:
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
            mismatched_factors=frozenset(mismatched),
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
        mismatched_factors=frozenset(),
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
    remaining = [factor for factor in ASK_ORDER if factor in pool and factor not in confirmed]
    return remaining[0] if remaining else None


# How many distinct values a caller may offer for one field. Two allows a single correction —
# people misspeak, and read the wrong line off a document — while a third is enumeration
# rather than memory (FR-006a).
DEFAULT_MAX_DISTINCT_VALUES = 2

# Truncated because the full digest is not needed to tell two attempts apart within one call,
# and a shorter one is less useful to anyone who later gets hold of the table.
_FINGERPRINT_LENGTH = 16


def fingerprint(factor: Factor, value: str, salt: str) -> str:
    """
    Reduces one attempted answer to a value that can be counted but not read.

    factor: which field was answered, so the same string offered for two different fields
            counts separately.
    value:  what the caller said, normalised the same way the comparison normalises it, so
            "0445012218" and "+41 44 501 22 18" are recognised as one attempt rather than
            two.
    salt:   a deployment secret. Without it a stored fingerprint of a date of birth could be
            brute-forced from a table dump in seconds, since the space is small enough to
            enumerate.

    Returns: a truncated HMAC. Distinct attempts can be counted without retaining what was
             guessed (FR-006c).
    """
    normalised = _normalise_for_comparison(factor, value)
    digest = hmac.new(
        salt.encode(), f"{factor.value}:{normalised}".encode(), hashlib.sha256
    ).hexdigest()
    return digest[:_FINGERPRINT_LENGTH]


def _normalise_for_comparison(factor: Factor, value: str) -> str:
    """Applies the same normalisation the matching rule uses, so a caller repeating one
    answer in a different form is not counted as a second attempt."""
    if factor is Factor.PHONE:
        return _normalise_phone(value)
    if factor is Factor.DATE_OF_BIRTH:
        return _normalise_date(value)
    return value.strip().casefold()


def is_enumerating(distinct_counts: dict[Factor, int], max_distinct: int) -> Factor | None:
    """
    Decides whether a caller has moved from correcting themselves to trying values.

    distinct_counts: how many distinct values have been offered for each field this call.
    max_distinct:    how many are allowed, from policy.

    Returns: the first field that exceeded the allowance, or None.

    Counted per field deliberately (FR-006b). Correcting a mistyped email must not consume
    the allowance for the customer identifier: an honest caller with an unusual surname
    already has more to correct than most, and should not be treated as an attacker for it.
    """
    for factor, count in sorted(distinct_counts.items()):
        if count > max_distinct:
            return factor
    return None

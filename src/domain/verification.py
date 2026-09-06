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
from datetime import date, datetime
from enum import StrEnum

# The number of trailing digits compared when checking a phone number. Swiss subscriber
# numbers are nine digits after the country code, so comparing the last nine makes
# +41 44 123 45 67, 044 123 45 67 and 0041441234567 the same number — which they are.
PHONE_SIGNIFICANT_DIGITS = 9

# Returned when a date cannot be read at all. Distinct from a wrong answer: a caller whose
# date we failed to parse has not told us anything, and must not be scored as mistaken.
UNPARSEABLE_DATE = "\x00unparseable"

# Date formats a spoken date might be transcribed into. ISO first because that is what the
# record holds and what the agent is instructed to produce.
DATE_FORMATS = (
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    # Spoken forms. A caller says "the twenty-fifth of July, nineteen sixty-nine" and the
    # transcript writes "25th of July, 1969" -- which matched nothing until these existed, so
    # every date said aloud was scored as a wrong answer.
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %y",
    "%d %b %y",
    # "March 12, '74". Said this way constantly, and unreadable until now: the apostrophe is
    # stripped before parsing, and %y reads 69-99 as 1969-1999, which is the right century
    # for anybody old enough to be paying invoices.
    "%B %d %y",
    "%b %d %y",
)

# Words a caller says that a date parser cannot. Ordinals only: "twenty-fifth" is a day,
# whereas spelled-out years are written as digits by every transcriber we have seen.
_SPOKEN_DAYS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
    "thirtieth": 30,
}


class Factor(StrEnum):
    """What a caller can be asked for.

    A name is deliberately absent: callers volunteer it in the first sentence of every call,
    so counting it would hand over a third of the bar for free, and first name and surname
    are not independent facts (FR-003b).

    The account opening year was removed for a related reason. It is a fact about the company
    rather than about the caller, so every employee knows it and it distinguishes nobody —
    and almost nobody remembers it, so it wasted an exchange before reaching a question that
    could be answered.
    """

    EMAIL = "email"
    PHONE = "phone"
    DATE_OF_BIRTH = "date_of_birth"
    CUSTOMER_ID = "customer_id"


# Facts about the caller rather than about the company they work for. At least one confirmed
# factor must come from this set (FR-003a).
#
# This is the line between "a contact of Apex Capital" and "somebody who knows about Apex
# Capital". The customer id is excluded because it is printed on every invoice and known to
# everyone at the company, so a caller producing only company facts has demonstrated
# familiarity with the business and nothing about who they are.
#
# The limitation, stated plainly: colleagues often know each other's details, so these
# factors cannot distinguish one listed contact from another. That is tolerable because
# every listed contact has identical access — impersonating a colleague gains nothing. It is
# not tolerable for someone who has left the company, and the control there is removing them
# from the CRM rather than anything this rule can do.
PERSONAL_FACTORS = frozenset(
    {
        Factor.EMAIL,
        Factor.PHONE,
        Factor.DATE_OF_BIRTH,
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
    personal_satisfied:     whether at least one confirmed factor was a fact about the
                            caller rather than about their company.
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
    personal_satisfied: bool
    is_failed_attempt: bool
    mismatched_factors: frozenset[Factor] = frozenset()


def _normalise_phone(value: str) -> str:
    """Reduces a phone number to its significant trailing digits, so written form and
    country-code prefix stop mattering."""
    digits = re.sub(r"\D", "", value)
    return digits[-PHONE_SIGNIFICANT_DIGITS:]


# Punctuation a caller names rather than pronounces. Spaces are required around each, so an
# address containing the letters is untouched: "aldotata@x.ch" and "cat@x.ch" survive.
# Hyphen maps to a hyphen and is then stripped with the ones separating spelled letters, which
# is what makes "alpina hyphen tech" and "alpinatech" the same address.
_SPOKEN_PUNCTUATION = {
    "dot": ".",
    "period": ".",
    "point": ".",
    "at": "@",
    "hyphen": "-",
    "dash": "-",
    "underscore": "_",
}


def _normalise_email(value: str) -> str:
    """
    Reduces an email to the form a spoken one can be compared against.

    Case goes, because nobody speaks capitals. Spoken punctuation becomes real punctuation:
    asked to spell an address, a caller says "S-T-E-P-H-A-N-E dot R-I-C-H-A-R-D at
    I-N-N-O-V-A-T-E-C-H dot C-H", and that is what the transcript carries. The words are
    converted first, then the spacing and hyphens that separate spelled letters are removed —
    in that order, or "dot" would be glued into the address it was separating.

    Hyphens go for a second reason: the transcript reliably loses the one in a domain like
    "alpina-tech.ch", which is what locked out the first voice test.

    Deliberately narrow rather than a fuzzy match. Two addresses differing only by a hyphen
    now collide, which is why an ambiguous lookup resolves nobody. An address whose local part
    genuinely contains the word "dot" or "at" also collides, which is rare enough to accept
    and would otherwise make every spelled-out address unreadable.
    """
    spoken = f" {value.strip().casefold()} "
    for word, symbol in _SPOKEN_PUNCTUATION.items():
        spoken = spoken.replace(f" {word} ", symbol)
    return re.sub(r"[\s-]", "", spoken)


def _spoken_to_digits(value: str) -> str:
    """
    Strips the parts of a spoken date that a parser cannot read.

    value: the date as transcribed.

    Returns: the same date with ordinals, filler words and punctuation removed, so
             "the 25th of July, 1969" becomes "25 July 1969".
    """
    text = value.strip().casefold().replace(",", " ").replace("'", "")
    for word, day in _SPOKEN_DAYS.items():
        text = re.sub(rf"\b(twenty[- ])?{word}\b", str(day + (20 if "twenty" in text else 0)), text)
    text = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", text)
    text = re.sub(r"\b(the|of|on)\b", " ", text)
    return " ".join(text.split())


def _cleaned_date(value: str) -> str:
    """
    Strips the punctuation a transcript puts round a spoken date.

    value: what the caller said, as transcribed.

    Returns: the same date with commas and apostrophes removed and runs of spaces collapsed.
             "March 12, '74" becomes "March 12 74", which the two-digit-year formats read.
             Kept separate from the raw attempt so an already-clean date is never altered.
    """
    return re.sub(r"\s{2,}", " ", re.sub(r"[,'\u2019]", " ", value)).strip()


def _normalise_date(value: str) -> str:
    """Parses a spoken-then-transcribed date into ISO form, or returns a sentinel that can
    never equal a stored date so an unreadable answer is never mistaken for a matching one."""
    for candidate in (value.strip(), _cleaned_date(value), _spoken_to_digits(value)):
        for fmt in DATE_FORMATS:
            try:
                parsed = datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
            # A two-digit year is read by %y as 2000-2068 for 00-68, so somebody born in 1958
            # saying "fifty-eight" lands in 2058. Nobody has been born in the future, so the
            # century is not a guess: it is the only reading that can be true.
            if parsed > date.today():
                parsed = parsed.replace(year=parsed.year - 100)
            return parsed.isoformat()
    return UNPARSEABLE_DATE


def ambiguous_date(value: str) -> tuple[str, str] | None:
    """
    Decides whether a spoken date could mean two different days.

    value: the date as transcribed.

    Returns: the two readings as ISO dates, day-first then month-first, or None when only one
             reading is possible.

    "11 6 1994" is the 11th of June to a Swiss caller and the 6th of November to an American
    transcriber, and nothing in the string says which. Detected without touching the record:
    this is a question about what the caller said, not about whether they are right, so
    asking them to clarify reveals nothing.
    """
    digits = re.findall(r"\d+", value)
    if len(digits) != 3:
        return None

    first, second, year = (int(d) for d in digits[:3])
    # A four-digit year in first position means the string is already unambiguous ISO.
    if len(digits[0]) == 4 or not (1 <= first <= 12 and 1 <= second <= 12) or first == second:
        return None

    try:
        day_first = date(year, second, first).isoformat()
        month_first = date(year, first, second).isoformat()
    except ValueError:
        return None
    return day_first, month_first


# How each detail is reduced before anything is compared, counted or looked up. One table
# rather than a switch in each caller: they were three, and they had already drifted — the
# comparison folded an email's case and the fingerprint did not, so a caller who repeated one
# address in two spellings spent two of their three attempts on it.
_NORMALISE = {
    Factor.PHONE: _normalise_phone,
    Factor.EMAIL: _normalise_email,
    Factor.DATE_OF_BIRTH: _normalise_date,
}


def normalise(factor: Factor, value: str) -> str:
    """
    Reduces one answer to the form everything else works in.

    factor: which detail it is.
    value:  what the caller said, or what the record holds.

    Returns: the normalised form. A factor with no rule of its own is trimmed and folded,
             which is right for the customer id and for anything added later.
    """
    return _NORMALISE.get(factor, lambda v: v.strip().casefold())(value)


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
    return normalise(factor, supplied) == normalise(factor, stored)


def lookup_key(factor: Factor, value: str) -> str:
    """
    Reduces an identifier to the form the table is indexed on.

    factor: EMAIL or PHONE, the two a caller can be found by.
    value:  what the caller said, or what the record holds.

    Returns: the normalised key.

    The same function serves the index, the seed and the comparison, because when they were
    separate they drifted: comparison normalised phone numbers to their trailing digits while
    the index was queried with the raw string, so a caller reciting their own number
    correctly could never be found by it.
    """
    return normalise(factor, value)


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
    unreadable: set[Factor] = set()

    for factor, value in supplied.items():
        # An unknown customer has no stored values, so every answer is wrong — which is
        # exactly how a wrong answer against a known customer looks. The gate must not
        # reveal whether a customer exists.
        if factor is Factor.DATE_OF_BIRTH and _normalise_date(value) == UNPARSEABLE_DATE:
            # Unreadable is not wrong. A date we could not parse tells us nothing about the
            # caller, and counting it as a mismatch discards the answers they got right --
            # which is how a caller with a correct email and phone was told zero were
            # confirmed. Neither confirmed nor mismatched: simply not yet answered.
            unreadable.add(factor)
            continue

        if factor in stored and _matches(factor, value, stored[factor]):
            confirmed.add(factor)
        else:
            mismatched.add(factor)

    personal_satisfied = bool(confirmed & PERSONAL_FACTORS)
    enough = len(confirmed) >= required_count

    if mismatched:
        status = VerificationStatus.FAILED
    elif enough and personal_satisfied:
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
            personal_satisfied=False,
            is_failed_attempt=True,
            mismatched_factors=frozenset(mismatched),
        )

    return VerificationResult(
        status=status,
        confirmed_count=len(confirmed),
        required_count=required_count,
        personal_satisfied=personal_satisfied,
        # Nothing more to ask once verification has succeeded.
        is_failed_attempt=False,
        mismatched_factors=frozenset(),
    )


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
    normalised = normalise(factor, value)
    digest = hmac.new(
        salt.encode(), f"{factor.value}:{normalised}".encode(), hashlib.sha256
    ).hexdigest()
    return digest[:_FINGERPRINT_LENGTH]


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

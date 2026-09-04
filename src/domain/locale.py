"""Rendering amounts and dates the way each language says them aloud.

The agent speaks its numbers rather than printing them, so the rule is what a person would
say, not what a formatter would write. A German speaker says "vierzehnter März"; a French one
says "le quatorze mars". Getting that wrong is not a typo on a screen, it is the agent
sounding foreign on the phone.
"""

from datetime import date
from decimal import Decimal

SUPPORTED = ("en", "de", "fr", "it")
FALLBACK = "de"

# Month names as each language says them. Written out rather than taken from the platform's
# locale data, which is not installed in a Lambda image and would fail differently there than
# it does here.
MONTHS = {
    "en": (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ),
    "de": (
        "Januar",
        "Februar",
        "März",
        "April",
        "Mai",
        "Juni",
        "Juli",
        "August",
        "September",
        "Oktober",
        "November",
        "Dezember",
    ),
    "fr": (
        "janvier",
        "février",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
    ),
    "it": (
        "gennaio",
        "febbraio",
        "marzo",
        "aprile",
        "maggio",
        "giugno",
        "luglio",
        "agosto",
        "settembre",
        "ottobre",
        "novembre",
        "dicembre",
    ),
}

# How the currency is named after the amount, as spoken.
CURRENCY = {"en": "francs", "de": "Franken", "fr": "francs", "it": "franchi"}

# The separator Switzerland uses in writing, per language. Spoken aloud it disappears, but the
# written form appears in tickets and handoffs that people read.
THOUSANDS = {"en": "'", "de": "'", "fr": "'", "it": "'"}


def normalise(language: str | None) -> str:
    """
    Reduces whatever arrived to one of the four languages.

    language: a code, possibly regional ("de-CH") or absent.

    Returns: one of SUPPORTED, falling back to German — the language of the largest part of
             the customer base, and the one a Swiss caller is least surprised by (FR-033a).
    """
    if not language:
        return FALLBACK
    base = str(language).strip().lower().replace("_", "-").split("-")[0]
    return base if base in SUPPORTED else FALLBACK


def money(amount: Decimal | float | int, language: str) -> str:
    """
    Writes an amount the way it is said.

    amount:   the value, in francs.
    language: which of the four.

    Returns: e.g. "4'200.00 francs" or "4'200.00 Franken". Never rounded and never
             approximated — the agent states exact figures or none (FR-035).
    """
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    whole, _, fraction = f"{abs(value):.2f}".partition(".")
    grouped = f"{int(whole):,}".replace(",", THOUSANDS[normalise(language)])
    sign = "-" if value < 0 else ""
    return f"{sign}{grouped}.{fraction} {CURRENCY[normalise(language)]}"


def spoken_date(value: date | str, language: str) -> str:
    """
    Writes a date the way it is said.

    value:    the date, or its ISO form.
    language: which of the four.

    Returns: "6 July 2026", "6. Juli 2026", "le 6 juillet 2026", "il 6 luglio 2026".
    """
    when = date.fromisoformat(str(value)) if not isinstance(value, date) else value
    code = normalise(language)
    month = MONTHS[code][when.month - 1]

    if code == "de":
        return f"{when.day}. {month} {when.year}"
    if code == "fr":
        return f"le {when.day} {month} {when.year}"
    if code == "it":
        return f"il {when.day} {month} {when.year}"
    return f"{when.day} {month} {when.year}"

"""The four prompts must say the same things.

A translation that drifts is worse than no translation: the agent then behaves differently
depending on which language a caller happens to speak, and the difference is invisible until
somebody calls in that language.
"""

import pathlib
import re

import pytest

PROMPTS = pathlib.Path(__file__).resolve().parents[2] / "agent" / "prompt"
LANGUAGES = ("en", "de", "fr", "it")

# Names that must appear identically in every language: a tool called by a translated name is
# a tool that is never called.
LITERALS = (
    "verify_identity",
    "check_factor",
    "get_account_context",
    "match_payment",
    "propose_allocation",
    "request_credit",
    "create_escalation",
    "recent_invoices",
    "payer_address",
    "existing_ticket_id",
    "IDENTITY_NOT_ESTABLISHED",
    "ADDRESS_DISCREPANCY",
    "VERIFIED",
    "UNDER_REVIEW",
    "ALREADY_UNDER_REVIEW",
    "REQUESTED",
    "SERVICE_UNAVAILABLE",
    "MATCHED",
    "NOT_MATCHED",
    "AMBIGUOUS",
    "LOCKED",
    "NO_MATCH",
    "INSUFFICIENT",
)


def _text(language: str) -> str:
    return (PROMPTS / f"{language}.md").read_text()


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_language_has_a_prompt(language):
    assert (PROMPTS / f"{language}.md").exists()


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_sections_are_the_same_shape(language):
    """Not the same words — the same structure. A missing section is a missing rule."""
    assert _text(language).count("\n## ") == _text("en").count("\n## ")


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("literal", LITERALS)
def test_tool_names_and_statuses_are_never_translated(language, literal):
    assert literal in _text(language), f"{language} is missing {literal}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_never_do_list_is_the_same_length(language):
    """Every prohibition in English exists in the others. One dropped in translation is a
    rule that holds for some callers and not for others."""

    def rules(text: str) -> int:
        tail = text.split("\n## ")[-1]
        return len(re.findall(r"^- ", tail, re.M))

    assert rules(_text(language)) == rules(_text("en"))


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_prompt_carries_a_stored_value(language):
    """A worked example with a real customer's details in it becomes an instruction the model
    follows, which is how the account opening year survived being removed."""
    text = _text(language)
    for canary in ("klaus.mueller@", "alpina-tech.ch", "1974-03-12", "044 501"):
        assert canary not in text, f"{language} carries {canary}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_language_carries_both_holiday_years(language):
    text = _text(language)
    assert "2026" in text and "2027" in text


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_movable_feasts_match_the_calendar(language):
    """Good Friday, Ascension, Whit Monday and Corpus Christi move with Easter, so the dates
    are computed rather than remembered. Easter is 5 April 2026 and 28 March 2027."""
    from datetime import date, timedelta

    def easter(year: int) -> date:
        a, b, c = year % 19, year // 100, year % 100
        d, e, g = b // 4, b % 4, (b - (b + 8) // 25 + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = c // 4, c % 4
        el = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * el) // 451
        return date(year, (h + el - 7 * m + 114) // 31, ((h + el - 7 * m + 114) % 31) + 1)

    text = _text(language)
    for year in (2026, 2027):
        for offset in (-2, 1, 39, 50, 60):
            day = easter(year) + timedelta(days=offset)
            # Day number and year both present somewhere; the month name is translated.
            assert str(day.day) in text and str(year) in text, f"{language} {day}"


# Each site, spelled as that language spells it. A shared holiday list would tell a caller the
# Ticino agency is open on the sixth of January, which it is not.
SITES = {
    "en": ("Fribourg", "Zug", "Ticino"),
    "de": ("Freiburg", "Zug", "Tessin"),
    "fr": ("Fribourg", "Zoug", "Tessin"),
    "it": ("Friburgo", "Zugo", "Ticino"),
}


@pytest.mark.parametrize("language", LANGUAGES)
def test_all_three_sites_are_named(language):
    """There is no such thing as a Swiss public holiday: they are cantonal, and the three
    sites close on different days."""
    text = _text(language)
    for site in SITES[language]:
        assert site in text, f"{language} does not name {site}"

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

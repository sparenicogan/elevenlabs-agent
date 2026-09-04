"""The shape of the verification procedure.

The procedure is configuration, not code, so nothing else would notice it changing. These
assert the properties it exists for -- each corresponds to something the agent actually did
wrong on a real call, which a prompt rule had already forbidden in bold and did not prevent.
"""

import json
import pathlib

import pytest

PROCEDURE = (
    pathlib.Path(__file__).resolve().parents[2] / "agent" / "procedures" / "verification.json"
)


@pytest.fixture
def steps() -> list[dict]:
    return json.loads(PROCEDURE.read_text())["steps"]


def test_it_asks_everything_before_it_checks_anything(steps):
    """Questions, then one call. Not a question-and-check loop: the loop is what gave the
    agent something to narrate after each answer. Asserted as a shape rather than an exact
    list, so adding or dropping a factor is an edit and not a test failure."""
    types = [s["type"] for s in steps]
    assert types.count("tool_call") == 1
    assert types.index("tool_call") == len(types) - 2
    assert types[-1] == "branch"
    assert set(types[1 : types.index("tool_call")]) == {"ask"}


def test_nothing_speaks_between_the_questions(steps):
    """The agent said "I couldn't confirm that" after a correct email. The prompt forbade
    exactly that phrase, in bold, and it happened anyway. Here it cannot: there is no step
    between the asks in which to say anything, and no tool result to say it about."""
    tool_at = [s["type"] for s in steps].index("tool_call")
    assert all(s["type"] == "ask" for s in steps[1:tool_at])


def test_the_email_is_read_back_before_it_is_used(steps):
    """The first voice call was lost to a hyphen the transcript dropped. This confirms the
    transcription, not the answer -- there is no record to check against yet, which is why
    the step can run every time without telling a caller whether they were right."""
    read_back = steps[2]["instruction"].lower()
    assert "read the address back" in read_back
    assert "letter by letter" in read_back


def test_the_read_back_precedes_the_remaining_questions(steps):
    """It has to correct the email while the caller is still on that subject."""
    asks = [s["instruction"].lower() for s in steps if s["type"] == "ask"]
    assert "email" in asks[0]
    assert any("phone" in a for a in asks[2:])
    assert any("date of birth" in a for a in asks[2:])


def test_the_removed_factor_is_asked_for_nowhere(steps):
    """The account opening year was removed as a factor and the agent kept asking for it,
    because a stale example in the prompt and a stale enum in the tool schema both still
    offered it."""
    assert "opening year" not in json.dumps(steps).lower()


def test_the_failing_path_does_not_explain_itself(steps):
    """Telling a caller which answer was wrong is the oracle the gate exists to deny them
    (FR-004). Only the refusal paths are checked: the success path has nothing to reveal."""
    branch = steps[-1]
    refusals = " ".join(
        step.get("instruction", "")
        for outcome in [b["steps"] for b in branch["branches"][1:]] + [branch["fallback"]]
        for step in outcome
    ).lower()
    assert "never say why" in refusals or "never say which detail" in refusals


def test_a_tool_failure_does_not_become_a_verification_failure(steps):
    """ "Cannot check" is not "not allowed" (FR-011). The handler must not tell a caller their
    identity could not be confirmed when what actually happened is that the backend was down."""
    tool_call = next(s for s in steps if s["type"] == "tool_call")
    fallback = tool_call["on_failure"]["fallback"]
    assert fallback, "a tool failure with no handler becomes a verification failure"
    text = " ".join(s.get("instruction", "") for s in fallback).lower()
    assert "could not complete" in text or "nothing about their account has changed" in text

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


def test_it_is_a_fixed_sequence_ending_in_one_tool_call(steps):
    """Three questions, then one call. Not a question-and-check loop: the loop is what gave
    the agent something to narrate after each answer."""
    assert [s["type"] for s in steps] == [
        "tell",
        "ask",
        "ask",
        "ask",
        "ask",
        "tool_call",
        "branch",
    ]


def test_nothing_speaks_between_the_questions(steps):
    """The agent said "I couldn't confirm that" after a correct email. The prompt forbade
    exactly that phrase, in bold, and it happened anyway. Here it cannot: there is no step
    between the asks in which to say anything, and no tool result to say it about."""
    between = steps[1:5]
    assert all(s["type"] == "ask" for s in between)


def test_the_email_is_read_back_before_it_is_used(steps):
    """The first voice call was lost to a hyphen the transcript dropped. This confirms the
    transcription, not the answer -- there is no record to check against yet, which is why
    the step can run every time without telling a caller whether they were right."""
    read_back = steps[2]["instruction"].lower()
    assert "read the address back" in read_back
    assert "letter by letter" in read_back
    assert "no record to check it against" in read_back


def test_the_read_back_precedes_the_remaining_questions(steps):
    """It has to correct the email while the caller is still on that subject."""
    assert steps[1]["instruction"].lower().count("email") >= 1
    assert "phone" in steps[3]["instruction"].lower()
    assert "date of birth" in steps[4]["instruction"].lower()


def test_the_removed_factor_is_asked_for_nowhere(steps):
    """The account opening year was removed as a factor and the agent kept asking for it,
    because a stale example in the prompt and a stale enum in the tool schema both still
    offered it."""
    assert "opening year" not in json.dumps(steps).lower()


def test_no_branch_reveals_which_detail_failed(steps):
    """Including the failure path. Telling a caller which answer was wrong is the oracle the
    gate exists to deny them (FR-004)."""
    branch = steps[-1]
    outcomes = [b["steps"] for b in branch["branches"]] + [branch["fallback"]]
    for outcome in outcomes:
        for step in outcome:
            text = step.get("instruction", "").lower()
            assert (
                "never say which detail" in text
                or "do not say why" in text
                or "do not recap" in text
            )


def test_a_tool_failure_does_not_become_a_verification_failure(steps):
    """ "Cannot check" is not "not allowed" (FR-011). The handler must not tell a caller their
    identity could not be confirmed when what actually happened is that the backend was down."""
    on_failure = steps[5]["on_failure"]["fallback"]
    assert on_failure[0]["type"] == "retry"
    assert "could not complete the check" in on_failure[-1]["instruction"].lower()
    assert "do not say whether any detail" in on_failure[-1]["instruction"].lower()

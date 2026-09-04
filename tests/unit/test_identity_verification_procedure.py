"""The shape of the identity-verification procedure.

Configuration, so nothing else would notice it changing. Each assertion corresponds to
something that actually went wrong on a call.
"""

import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROCEDURE = ROOT / "agent" / "procedures" / "identity-verification.json"


@pytest.fixture
def steps() -> list[dict]:
    return json.loads(PROCEDURE.read_text())["steps"]


def _branch_for(step: dict, outcome: str) -> dict:
    """The branch handling one outcome, found by what it is for rather than its position."""
    return next(b for b in step["branches"] if outcome in b["condition"]["condition"])


def _tools(steps: list[dict]) -> list[str]:
    """Every tool named anywhere in the procedure, including inside branches."""
    found = []
    for step in steps:
        if step.get("type") == "tool_call":
            found.append(step["tool_name"])
        for branch in step.get("branches", []):
            found += _tools(branch["steps"])
        found += _tools(step.get("fallback", []))
    return found


def test_each_detail_is_checked_while_the_caller_is_still_on_it(steps):
    """A misheard surname is recoverable in the moment and not afterwards."""
    order = [s.get("tool_name") for s in steps if s["type"] == "tool_call"]
    assert order == ["check_factor", "check_factor", "check_factor", "verify_identity"]


def test_verify_identity_runs_last_and_once(steps):
    """The per-factor checks recover from transcription. They decide nothing."""
    assert _tools(steps).count("verify_identity") == 1
    assert steps[-2]["tool_name"] == "verify_identity"


def test_a_misheard_email_is_spelled_out_rather_than_guessed(steps):
    recovery = _branch_for(steps[3], "NOT_MATCHED")
    instruction = recovery["steps"][0]["instruction"].lower()
    assert "spell the address out" in instruction
    assert "do not suggest a correction" in instruction
    assert "do not say it was wrong" in instruction
    assert _tools(recovery["steps"]) == ["check_factor"]


def test_an_ambiguous_date_is_clarified_by_naming_the_month(steps):
    """ "11 6 1994" is two different days. Naming the month in words is the only way to ask
    that does not depend on which order the caller assumes."""
    clarify = _branch_for(steps[8], "AMBIGUOUS")["steps"][0]["instruction"].lower()
    # Both readings offered by name, so the answer does not depend on which order the caller
    # assumes the numbers were in.
    assert "june" in clarify and "november" in clarify


def test_the_failing_path_does_not_explain_itself(steps):
    """FR-004. The per-factor checks are an oracle by design; the final refusal is not."""
    final = steps[-1]
    # Selected by what the branch is for, not by position: a branch added at the front for an
    # outage should not silently move the assertion onto a different outcome.
    refusing = [
        b["steps"] for b in final["branches"] if "VERIFIED" not in b["condition"]["condition"]
    ]
    refusals = " ".join(
        s.get("instruction", "") for outcome in refusing + [final["fallback"]] for s in outcome
    ).lower()
    assert "never say why" in refusals


def test_both_decisions_survive_the_backend_being_down(steps):
    """ "Cannot check" is not "not allowed" (FR-011)."""
    for step in steps:
        if step.get("tool_name") in {"check_factor", "verify_identity"} and "on_failure" in step:
            fallback = step["on_failure"]["fallback"]
            assert "retry" not in [s["type"] for s in fallback]
            text = " ".join(s.get("instruction", "") for s in fallback)
            assert "nothing has changed" in text.lower()


def test_an_outage_is_never_reported_as_a_failed_check(steps):
    """The handlers answer 200 with SERVICE_UNAVAILABLE in the body, so an outage arrives as
    a result rather than as a tool failure. Without a branch for it the agent is left reading
    a status it has no instruction for, next to instructions about details not matching."""
    for step in steps:
        if step["type"] != "branch":
            continue
        unavailable = [
            b for b in step["branches"] if "SERVICE_UNAVAILABLE" in b["condition"]["condition"]
        ]
        assert unavailable, "a branch with no outage path"
        said = unavailable[0]["steps"][0]["instruction"].lower()
        assert "nothing has changed" in said


def test_the_removed_factor_is_asked_for_nowhere(steps):
    assert "opening year" not in json.dumps(steps).lower()

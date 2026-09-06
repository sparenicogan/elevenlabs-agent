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
    recovery = steps[3]["branches"][0]
    instruction = recovery["steps"][0]["instruction"].lower()
    assert "letter by letter" in instruction
    assert "do not suggest a correction" in instruction
    assert _tools(recovery["steps"]) == ["check_factor"]


def test_an_ambiguous_date_is_clarified_by_naming_the_month(steps):
    """ "11 6 1994" is two different days. Naming the month in words is the only way to ask
    that does not depend on which order the caller assumes."""
    clarify = steps[8]["branches"][0]["steps"][0]["instruction"].lower()
    assert "month in words" in clarify
    assert "not about whether it is right" in clarify


def test_the_failing_path_does_not_explain_itself(steps):
    """FR-004. The per-factor checks are an oracle by design; the final refusal is not."""
    final = steps[-1]
    refusals = " ".join(
        s.get("instruction", "")
        for outcome in [b["steps"] for b in final["branches"][1:]] + [final["fallback"]]
        for s in outcome
    ).lower()
    assert "never say why" in refusals
    assert "never say which detail" in refusals


def test_both_decisions_survive_the_backend_being_down(steps):
    """ "Cannot check" is not "not allowed" (FR-011)."""
    for step in steps:
        if step.get("tool_name") in {"check_factor", "verify_identity"} and "on_failure" in step:
            text = " ".join(s.get("instruction", "") for s in step["on_failure"]["fallback"])
            assert "nothing has changed" in text.lower()


def test_the_removed_factor_is_asked_for_nowhere(steps):
    assert "opening year" not in json.dumps(steps).lower()


class TestAnUnreadableDateIsNotAMatch:
    """conv_0901m1veyhwbe7qvxm6rfn22pf71. A caller gave a correct email, a correct phone and
    "March 12, '74". All three per-detail checks said MATCHED; verify_identity then confirmed
    two of three and refused him. He was who he said he was.

    check_factor asked whether the answer was *mismatched*, and an unreadable date is
    deliberately neither confirmed nor mismatched -- that is what stops it discarding the
    answers a caller got right. Reading that absence as a match meant the one endpoint whose
    whole job is to catch a misheard detail reported the misheard detail as fine.
    """

    def test_an_unreadable_date_is_not_confirmed(self):
        from src.domain.verification import Factor, check_factors

        outcome = check_factors(
            supplied={Factor.DATE_OF_BIRTH: "sometime in seventy four"},
            stored={Factor.DATE_OF_BIRTH: "1958-11-30"},
            required_count=1,
        )
        assert outcome.confirmed_count == 0
        # Still not a mismatch: it tells us nothing about the caller either way.
        assert not outcome.mismatched_factors

    def test_check_factor_asks_whether_it_was_confirmed(self):
        """The distinction the bug turned on, held in place."""
        import inspect

        from src.handlers import check_factor

        source = inspect.getsource(check_factor._compares)
        assert "confirmed_count == 1" in source
        assert "not outcome.mismatched_factors" not in source


class TestDatesSaidAloud:
    """Every one of these was said on a real call or is a turn of phrase people use. The
    model is asked to send yyyy-mm-dd and usually will; this is the backstop for when it
    does not, because the cost of the model not complying is a real customer refused."""

    def test_the_forms_a_caller_actually_uses_all_parse(self):
        from src.domain.verification import Factor, normalise

        for spoken in (
            "1958-11-30",
            "30.11.1958",
            "30/11/1958",
            "30 November 1958",
            "November 30, 1958",
            "November 30, '58",
            "30th of November, 1958",
        ):
            assert normalise(Factor.DATE_OF_BIRTH, spoken) == "1958-11-30", spoken

    def test_something_nobody_can_read_stays_unreadable(self):
        """It must never accidentally resolve to a date, or an unreadable answer becomes a
        wrong one and a caller is failed for the transcription rather than the answer."""
        from src.domain.verification import UNPARSEABLE_DATE, Factor, normalise

        for noise in ("sometime in seventy four", "the thirtieth", "uhh November"):
            assert normalise(Factor.DATE_OF_BIRTH, noise) == UNPARSEABLE_DATE, noise

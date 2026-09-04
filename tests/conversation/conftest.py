"""Running scripted conversations against the deployed agent.

This is the only layer that tests the agent itself. Everything else stops at the handler
boundary: the unit tests exercise pure rules, the contract tests exercise handlers with the
world stubbed out, and the integration tests call HTTP endpoints directly. None of them has
ever seen the prompt.

That matters because the prompt is a security control. "Never say which factor was wrong",
"never say the invoice amount before asking what they paid", "never claim to have checked
something" — twenty-six thousand characters of instruction that the model may or may not
follow, and until now the only way to find out was to talk to it.

These tests use ElevenLabs' conversation simulator, which drives a scripted caller against
the real agent with the real prompt and the real tools. No voice minutes are consumed.

    AWS_PROFILE=voice-agent-admin uv run pytest tests/conversation -v

**They mutate real data.** The agent calls the same endpoints a phone call does, so a
simulated caller can be granted a real credit. The fixtures are reseeded around every test.

**They are not deterministic, and that is the finding rather than a defect.** The same
scenario run twice can pass and then fail: a rule the model followed at 14:02 it ignored at
14:05. Three of these failed on their second run having passed on their first.

That is worth stating plainly, because it is the difference between the two kinds of rule in
this system. "Nothing financial before VERIFIED" holds because `require_verified` reads a row
and raises — no amount of talking changes it. "Never say an action succeeded when it did not"
holds because the model usually complies. The first is a control; the second is a tendency.

So a failure here is information about the prompt, not necessarily a regression. When one
appears, the question is whether the rule can be moved somewhere it will be enforced — the
directive `message_hint` in `adapters/errors.py` exists because of exactly such a failure —
and where it cannot, whether the residual risk is acceptable and recorded.
"""

import re
import subprocess

import httpx
import pytest

AGENT_ID = "agent_3501m1k6hy8yech93pn3gfets2tx"
API = "https://api.elevenlabs.io/v1"

# A simulated conversation is slow — each turn is a model call — and a long one drifts from
# the scenario. Twelve turns is enough for verification plus the thing being tested.
TURN_LIMIT = 12

# Generous, because a simulated conversation is a sequence of model calls and a sixteen-turn
# one can run for several minutes. This layer is run deliberately rather than on every
# commit, so waiting is cheaper than a flaky timeout.
TIMEOUT = 420.0


def _shell(*command: str) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


@pytest.fixture(scope="session")
def api_key() -> str:
    """The ElevenLabs key, from Secrets Manager rather than any file."""
    key = _shell(
        "aws",
        "secretsmanager",
        "get-secret-value",
        "--secret-id",
        "voice-agent/elevenlabs/api-key",
        "--query",
        "SecretString",
        "--output",
        "text",
    )
    if not key:
        pytest.skip("no AWS credentials, or the ElevenLabs key is not set")
    return key


@pytest.fixture
def seeded():
    """Restores the fixtures before the test, so a previous simulation's credits and
    allocations do not change what this one sees."""
    _shell("uv", "run", "python", "-m", "scripts.seed.seed", "--env", "dev")
    yield


class Transcript:
    """One simulated conversation, with the questions worth asking of it."""

    def __init__(self, turns: list[dict]):
        self.turns = turns

    @property
    def agent_said(self) -> str:
        """Everything the agent said, joined. The thing most assertions look at."""
        return "\n".join((t.get("message") or "") for t in self.turns if t.get("role") == "agent")

    def tools_called(self) -> list[str]:
        """Every tool the agent invoked, in order."""
        return [call["tool_name"] for turn in self.turns for call in (turn.get("tool_calls") or [])]

    def said_before_tool(self, tool: str) -> str:
        """
        Everything the agent said before it first called a named tool.

        tool: the tool name.

        Returns: the agent's words up to that point. Used to check ordering — that a figure
                 was not spoken before the tool that establishes it was called.
        """
        said: list[str] = []
        for turn in self.turns:
            if any(c["tool_name"] == tool for c in (turn.get("tool_calls") or [])):
                break
            if turn.get("role") == "agent":
                said.append(turn.get("message") or "")
        return "\n".join(said)

    def mentions_money(self, text: str | None = None) -> list[str]:
        """
        Finds spoken monetary amounts.

        text: what to search, defaulting to everything the agent said.

        Returns: the amounts found, digits and words both — "4,200", "four thousand two
                 hundred". Written out is how an agent says a number aloud, so a check that
                 only looked for digits would miss every real disclosure.
        """
        haystack = (text if text is not None else self.agent_said).lower()

        # Invoice numbers and dates contain digits and are meant to be spoken. Removed
        # first, or "INV-2026-0013" reads as two amounts and every ordering test fails on
        # the agent doing exactly what it was told.
        haystack = re.sub(r"inv-\d{4}-\d{4}", " ", haystack)
        haystack = re.sub(r"\b(19|20)\d{2}\b", " ", haystack)

        # A figure counts as money when it is said as money: next to a currency, or written
        # out the way an amount is spoken aloud. A bare number in a sentence is usually a
        # date, a count, or part of a reference.
        digits = re.findall(
            r"\b\d[\d,.']*\s*(?:francs?|chf)\b|\b(?:chf|francs?)\s*\d[\d,.']*", haystack
        )
        words = re.findall(
            r"\b(?:one|two|three|four|five|six|seven|eight|nine)\s+"
            r"(?:thousand|hundred)[\w\s]{0,40}?(?:francs?|chf)",
            haystack,
        )
        return digits + words

    def __str__(self) -> str:
        lines = []
        for turn in self.turns:
            message = (turn.get("message") or "").strip()
            if message:
                lines.append(f"[{turn.get('role', '?')}] {message}")
            for call in turn.get("tool_calls") or []:
                lines.append(f"    -> {call['tool_name']}")
        return "\n".join(lines)


@pytest.fixture
def simulate(api_key):
    """
    Runs a scripted caller against the deployed agent.

    Returns a callable taking the caller's persona and opening line, and returning a
    Transcript. The persona is a prompt for the simulated user, so it should describe who
    they are and what they will and will not do — not a script, because the point is to see
    how the agent handles a person rather than a recording.
    """
    client = httpx.Client(headers={"xi-api-key": api_key}, timeout=TIMEOUT)

    def run(persona: str, first_message: str, turns: int = TURN_LIMIT) -> Transcript:
        response = client.post(
            f"{API}/convai/agents/{AGENT_ID}/simulate-conversation",
            json={
                "simulation_specification": {
                    "simulated_user_config": {
                        "prompt": {"prompt": persona},
                        "first_message": first_message,
                        "language": "en",
                    },
                    # Supplied explicitly: there is no real call, so the platform has no
                    # conversation to name.
                    "dynamic_variables": {
                        "system__conversation_id": f"sim_{abs(hash(persona)) % 10**10}"
                    },
                },
                "new_turns_limit": turns,
            },
        )
        assert response.status_code == 200, response.text[:500]
        return Transcript(response.json()["simulated_conversation"])

    return run


def assert_no_money_spoken(transcript: Transcript, text: str, context: str) -> None:
    """
    Asserts no monetary amount appears in a stretch of the agent's speech.

    transcript: the conversation, for the failure message.
    text:       the stretch to check.
    context:    what was being tested, so a failure explains itself.
    """
    amounts = transcript.mentions_money(text)
    assert not amounts, f"{context}: agent spoke {amounts}\n\n{transcript}"

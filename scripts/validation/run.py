"""Runs each user story against the deployed agent and writes up what happened.

One markdown file per story: the transcript, the tools the agent actually called, and each
acceptance check with a verdict and the reason it matters. A failing check is recorded rather
than retried, because the interesting question is not whether the agent can pass but whether
it does.

    AWS_PROFILE=voice-agent-admin uv run python -m scripts.validation.run
    AWS_PROFILE=voice-agent-admin uv run python -m scripts.validation.run --story US1
"""

import argparse
import pathlib
import re
import subprocess
from datetime import UTC, datetime

import httpx

from scripts.validation.scenarios import SCENARIOS, Scenario

AGENT_ID = "agent_3501m1k6hy8yech93pn3gfets2tx"
API = "https://api.elevenlabs.io/v1"
OUT = pathlib.Path("docs/validation")

# A simulated turn is a model call, and a long conversation drifts from its scenario.
TIMEOUT = 600.0


def _shell(*command: str) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


# Numbers as an agent says them aloud, which is how an amount reaches a transcript.
_SPOKEN_NUMBER = (
    "(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    "|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)"
)


class Transcript:
    """One finished conversation, with the questions worth asking of it."""

    def __init__(self, turns: list[dict]):
        self.turns = turns

    @property
    def agent_said(self) -> str:
        return "\n".join((t.get("message") or "") for t in self.turns if t.get("role") == "agent")

    def tools_called(self) -> list[str]:
        return [c["tool_name"] for t in self.turns for c in (t.get("tool_calls") or [])]

    def said_before_tool(self, tool: str) -> str:
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

        Returns: the amounts found, digits and words both. Invoice numbers and years are
                 removed first, or "INV-2026-0013" reads as two amounts and the agent is
                 marked down for doing exactly what it was told.
        """
        haystack = (text if text is not None else self.agent_said).lower()
        haystack = re.sub(r"inv-\d{4}-\d{4}", " ", haystack)
        haystack = re.sub(r"\b(19|20)\d{2}\b", " ", haystack)
        # An amount counts when it is said as one: beside a currency, or written out the way
        # a number is spoken aloud. A bare number in a sentence is usually a date or a count.
        spoken = _SPOKEN_NUMBER
        return re.findall(
            rf"(?:chf|francs?)\s*[\d,.']+"
            rf"|[\d,.']+\s*(?:chf|francs?)"
            rf"|{spoken}(?:[\s-]+{spoken})*\s+francs?",
            haystack,
        )


def simulate(scenario: Scenario, api_key: str) -> Transcript:
    """Runs one scenario against the deployed agent, consuming no voice minutes."""
    response = httpx.post(
        f"{API}/convai/agents/{AGENT_ID}/simulate-conversation",
        headers={"xi-api-key": api_key},
        timeout=TIMEOUT,
        json={
            "simulation_specification": {
                "simulated_user_config": {
                    "prompt": {"prompt": scenario.persona},
                    "first_message": scenario.opening,
                    "language": "en",
                },
                "dynamic_variables": {
                    "system__conversation_id": f"val_{scenario.story.lower()}_"
                    f"{datetime.now(UTC).strftime('%H%M%S')}"
                },
            },
            "new_turns_limit": scenario.turns,
        },
    )
    if response.status_code != 200:
        raise SystemExit(f"simulate failed: {response.status_code}\n{response.text[:400]}")
    return Transcript(response.json()["simulated_conversation"])


def write_report(scenario: Scenario, transcript: Transcript, results: list[tuple]) -> pathlib.Path:
    """
    Writes one story's result to docs/validation.

    Returns: the path written. The transcript goes in whole: a summary of a conversation is
             an opinion about it, and the point of this file is that somebody else can form
             their own.
    """
    passed = sum(1 for _, ok, _ in results if ok)
    verdict = "PASSED" if passed == len(results) else "FAILED"

    lines = [
        f"# {scenario.story} — {scenario.title}",
        "",
        f"**{verdict}** — {passed} of {len(results)} checks. "
        f"Run {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC against the deployed agent.",
        "",
        "## Checks",
        "",
        "| | Check | Why it matters |",
        "|---|---|---|",
    ]
    for name, ok, why in results:
        lines.append(f"| {'PASS' if ok else 'FAIL'} | {name} | {why} |")

    tools = transcript.tools_called()
    lines += [
        "",
        "## Tools called",
        "",
        "```",
        "\n".join(f"{i:2}. {t}" for i, t in enumerate(tools, 1)) if tools else "none",
        "```",
        "",
        "## Transcript",
        "",
    ]
    for turn in transcript.turns:
        message = (turn.get("message") or "").strip()
        called = [c["tool_name"] for c in (turn.get("tool_calls") or [])]
        if message:
            lines.append(f"**{turn.get('role', '?')}** — {message}")
            lines.append("")
        for tool in called:
            lines.append(f"> called `{tool}`")
            lines.append("")

    if scenario.notes:
        lines += ["## Notes", "", scenario.notes, ""]

    path = OUT / f"{scenario.story.lower()}.md"
    path.write_text("\n".join(lines).rstrip() + "\n")
    return path


def main() -> int:
    """
    Runs the scenarios and writes one report each.

    Returns: 0 when every check passed, 1 otherwise — so this can gate a release without
             anybody having to read the files first.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--story", help="run one, e.g. US1")
    args = parser.parse_args()

    api_key = _shell(
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
    if not api_key:
        raise SystemExit("no ElevenLabs key; run aws sso login first")

    OUT.mkdir(parents=True, exist_ok=True)
    chosen = [s for s in SCENARIOS if not args.story or s.story == args.story]
    if not chosen:
        raise SystemExit(f"no scenario named {args.story}")

    failures = 0
    for scenario in chosen:
        # Reseeded before each: a previous simulation's credit changes what the next one sees.
        _shell("uv", "run", "python", "-m", "scripts.seed.seed", "--env", "dev")
        print(f"{scenario.story}: running...", flush=True)

        transcript = simulate(scenario, api_key)
        results = [(c.name, bool(c.passed(transcript)), c.why) for c in scenario.checks]
        path = write_report(scenario, transcript, results)

        passed = sum(1 for _, ok, _ in results if ok)
        failures += len(results) - passed
        print(f"  {passed}/{len(results)} checks — {path}")
        for name, ok, _ in results:
            if not ok:
                print(f"    FAILED: {name}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

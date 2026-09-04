"""Pushes the structured procedures in agent/procedures to ElevenLabs.

A procedure is a fixed sequence the platform enforces rather than the model interprets: an
Ask step does not advance until it has been answered, and a Tool step runs without the model
deciding to. That is the difference between the verification order being a rule in a prompt
and it being a property of the call.

Kept separate from sync.py because publishing recompiles the agent's workflow, which is a
larger change than updating a prompt and should be run deliberately.
"""

import argparse
import json

import httpx

from scripts.agent.sync import API, ROOT, secret

PROCEDURES = ROOT / "agent" / "procedures"
TIMEOUT = 30.0


class Procedures:
    """The procedure endpoints, which are not part of the tool or agent APIs."""

    def __init__(self, api_key: str, agent_id: str):
        self._client = httpx.Client(headers={"xi-api-key": api_key}, timeout=TIMEOUT)
        self._agent = agent_id
        # Procedures hang off a branch, not the account: edits stay private to the branch
        # until an agent version is published, which is what makes pushing a draft safe to
        # run against a live agent.
        branch = self._call("GET", f"/convai/agents/{agent_id}")["main_branch_id"]
        self._base = f"/convai/agents/{agent_id}/branches/{branch}/procedures"

    def _call(self, method: str, path: str, **kwargs) -> dict:
        response = self._client.request(method, f"{API}{path}", **kwargs)
        if response.status_code >= 400:
            raise SystemExit(f"{method} {path} -> {response.status_code}\n{response.text[:800]}")
        return response.json() if response.content else {}

    def existing(self) -> dict[str, str]:
        """Procedure name to id, so a sync updates in place rather than duplicating."""
        listed = self._call("GET", self._base).get("procedures", [])
        return {p["name"]: p["procedure_id"] for p in listed}

    def upsert(self, name: str, document: dict, existing: dict[str, str]) -> str:
        """
        Creates or updates one procedure.

        name:     the dashboard label. Not sent to the model.
        document: the trigger and steps, which are sent as a JSON-encoded string.
        existing: name to id, from existing().

        Returns: the procedure id.
        """
        payload = {
            "name": name,
            "type": "deterministic",
            "trigger": document["trigger"],
            "content": json.dumps(document),
        }
        if name in existing:
            self._call("PATCH", f"{self._base}/{existing[name]}/draft", json=payload)
            return existing[name]
        return self._call("POST", self._base, json=payload)["procedure_id"]

    def tool_ids(self) -> dict[str, str]:
        """Tool name to id, for resolving the tool a Tool step names."""
        tools = self._call("GET", "/convai/tools")["tools"]
        return {t["tool_config"]["name"]: t["id"] for t in tools}


def _resolve_tools(document: dict, tool_ids: dict[str, str]) -> dict:
    """
    Fills in tool_id wherever a step names a tool.

    document: the procedure, with tool_name on its Tool steps.
    tool_ids: tool name to id, from the live account.

    Returns: the same document with tool_id set. Written by name in the file and resolved
             here, so the checked-in procedure stays readable and survives a tool being
             recreated with a new id.
    """

    def walk(steps: list) -> list:
        for step in steps:
            if step.get("type") == "tool_call":
                step["tool_id"] = tool_ids[step["tool_name"]]
            for key in ("steps", "fallback"):
                if key in step:
                    walk(step[key])
            for branch in step.get("branches", []):
                walk(branch["steps"])
            if "on_failure" in step:
                walk(step["on_failure"].get("fallback", []))
                for branch in step["on_failure"].get("branches", []):
                    walk(branch["steps"])
        return steps

    document["steps"] = walk(document["steps"])
    return document


def main() -> int:
    """
    Pushes every procedure in agent/procedures as a draft.

    Returns: 0 on success. Drafts are private until the agent version is published, so this
             never changes what a caller reaches on its own.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    files = sorted(PROCEDURES.glob("*.json"))
    if args.dry_run:
        for path in files:
            steps = [s["type"] for s in json.loads(path.read_text())["steps"]]
            print(f"would push {path.stem}: {len(steps)} steps {steps}")
        return 0

    client = Procedures(secret("voice-agent/elevenlabs/api-key"), args.agent_id)
    tool_ids = client.tool_ids()
    existing = client.existing()

    for path in files:
        document = _resolve_tools(json.loads(path.read_text()), tool_ids)
        procedure_id = client.upsert(path.stem, document, existing)
        print(f"pushed {path.stem} -> {procedure_id} ({len(document['steps'])} steps)")

    print("drafts only. Compile and publish the agent version to make them live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Pushes the agent configuration in this repository to ElevenLabs.

The prompt and the tool definitions are the security boundary the model actually reads, so
they belong in version control and code review rather than in a dashboard where a change
leaves no trace. This script is the only way they reach ElevenLabs.

Idempotent: tools are matched by name and updated in place, so running it twice changes
nothing and running it after an edit changes exactly what was edited.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
API = "https://api.elevenlabs.io/v1"
TIMEOUT = 30.0


def secret(name: str) -> str:
    """Reads a value from Secrets Manager, never from the repository or the environment."""
    return subprocess.run(
        [
            "aws",
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            name,
            "--query",
            "SecretString",
            "--output",
            "text",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def terraform_output(name: str) -> str:
    """Reads a deployed value from Terraform state, so no endpoint is hardcoded here."""
    return subprocess.run(
        ["terraform", f"-chdir={ROOT}/infra/terraform", "output", "-raw", name],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class ElevenLabs:
    """The subset of the ElevenLabs API this project uses."""

    def __init__(self, api_key: str):
        self._client = httpx.Client(headers={"xi-api-key": api_key}, timeout=TIMEOUT)

    def _call(self, method: str, path: str, **kwargs) -> dict:
        response = self._client.request(method, f"{API}{path}", **kwargs)
        if response.status_code >= 400:
            raise SystemExit(f"{method} {path} -> {response.status_code}\n{response.text[:800]}")
        return response.json() if response.content else {}

    def existing_tools(self) -> dict[str, str]:
        """Tool name to id, so a sync updates in place rather than duplicating."""
        tools = self._call("GET", "/convai/tools")["tools"]
        return {t["tool_config"]["name"]: t["id"] for t in tools}

    def upsert_tool(self, config: dict, existing: dict[str, str]) -> str:
        name = config["name"]
        if name in existing:
            self._call("PATCH", f"/convai/tools/{existing[name]}", json={"tool_config": config})
            return existing[name]
        return self._call("POST", "/convai/tools", json={"tool_config": config})["id"]

    def update_agent(self, agent_id: str, payload: dict) -> dict:
        return self._call("PATCH", f"/convai/agents/{agent_id}", json=payload)

    def secret_id(self, name: str) -> str:
        for entry in self._call("GET", "/convai/secrets")["secrets"]:
            if entry["name"] == name:
                return entry["secret_id"]
        raise SystemExit(f"workspace secret not found: {name}")


def build_tools(base_url: str, tool_secret_id: str) -> list[dict]:
    """
    Renders the tool definitions against the deployed endpoint.

    base_url:       API Gateway base, from Terraform output.
    tool_secret_id: workspace secret holding the tool API key. Referenced rather than
                    inlined, so the key exists in Secrets Manager and in the ElevenLabs
                    secret store, and in no file.

    Returns: one tool_config per tool, in ElevenLabs' schema.
    """
    spec = json.loads((ROOT / "agent" / "tools.json").read_text())
    for tool in spec:
        tool["api_schema"]["url"] = f"{base_url.rstrip('/')}{tool['api_schema']['url']}"
        tool["api_schema"]["request_headers"] = {"x-api-key": {"secret_id": tool_secret_id}}
    return spec


def main() -> int:
    """
    Syncs tools and the agent's prompt.

    Returns: 0 on success. --dry-run prints what would change without calling ElevenLabs'
             write endpoints.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    client = ElevenLabs(secret("voice-agent/elevenlabs/api-key"))
    tools = build_tools(
        terraform_output("api_endpoint"), client.secret_id("voice_agent_tool_api_key")
    )
    agent_config = json.loads((ROOT / "agent" / "agent.json").read_text())
    prompt = (ROOT / "agent" / "prompt" / "en.md").read_text()

    if args.dry_run:
        print(f"would sync {len(tools)} tools and a {len(prompt)}-character prompt")
        for tool in tools:
            print(f"  {tool['name']:22} {tool['api_schema']['url']}")
        return 0

    existing = client.existing_tools()
    tool_ids = [client.upsert_tool(tool, existing) for tool in tools]

    conversation_config = json.loads(json.dumps(agent_config["conversation_config"]))
    conversation_config["agent"]["prompt"]["prompt"] = prompt
    conversation_config["agent"]["prompt"]["tool_ids"] = tool_ids

    client.update_agent(
        args.agent_id,
        {"name": agent_config["name"], "conversation_config": conversation_config},
    )
    print(f"synced {len(tool_ids)} tools and a {len(prompt)}-character prompt")
    print(f"agent {args.agent_id} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())

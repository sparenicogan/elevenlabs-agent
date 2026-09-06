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

    def agent(self, agent_id: str) -> dict:
        return self._call("GET", f"/convai/agents/{agent_id}")

    def update_agent(self, agent_id: str, payload: dict) -> dict:
        return self._call("PATCH", f"/convai/agents/{agent_id}", json=payload)

    def knowledge_documents(self) -> list[dict]:
        """Every document in the workspace, so ours can be matched by name."""
        return self._call("GET", "/convai/knowledge-base").get("documents", [])

    def create_knowledge_document(self, name: str, text: str) -> str:
        """Uploads one document and returns its id."""
        return self._call("POST", "/convai/knowledge-base/text", json={"name": name, "text": text})[
            "id"
        ]

    def delete_knowledge_document(self, document_id: str) -> None:
        """Removes a superseded document. Called after the replacement is attached, never
        before -- the workspace already carries two copies of one file from a sync that
        created without cleaning up."""
        self._client.request("DELETE", f"{API}/convai/knowledge-base/{document_id}")

    def workspace_webhooks(self) -> list[dict]:
        return self._call("GET", "/workspace/webhooks").get("webhooks", [])

    def update_webhook(
        self, webhook_id: str, name: str, retry_enabled: bool, is_disabled: bool
    ) -> None:
        """
        Applies the delivery settings this repository declares.

        is_disabled is passed back exactly as it was found, never as a decision. The API
        requires the field -- omitting it answers 422 -- but a webhook auto-disables after ten
        consecutive failures, and a deploy that silently switched it back on would hide the
        thing that disabled it and spend another ten deliveries finding out. Re-enabling stays
        a deliberate act by somebody who has read the failure.
        """
        self._call(
            "PATCH",
            f"/workspace/webhooks/{webhook_id}",
            json={
                "name": name,
                "retry_enabled": retry_enabled,
                "is_disabled": is_disabled,
            },
        )

    def secret_id(self, name: str) -> str:
        for entry in self._call("GET", "/convai/secrets")["secrets"]:
            if entry["name"] == name:
                return entry["secret_id"]
        raise SystemExit(f"workspace secret not found: {name}")


def publish_knowledge(client: "ElevenLabs", declared: dict) -> tuple[list[dict], list[str]]:
    """
    Uploads the knowledge base this repository declares and says what to attach.

    client:   the API client.
    declared: the knowledge_base block from agent.json -- which files, and how they are used.

    Returns: (attachments for the agent, ids of the documents they replace).

    A text document cannot be edited in place, so each sync creates a new one and the old is
    deleted only after the agent points at the replacement. Doing it the other way round would
    leave the agent referring to a document that no longer exists.
    """
    superseded = [
        d["id"]
        for d in client.knowledge_documents()
        if d.get("name") in set(declared.get("documents") or [])
    ]

    attachments = []
    for filename in declared.get("documents") or []:
        text = (ROOT / "agent" / "knowledge" / filename).read_text()
        attachments.append(
            {
                "type": "file",
                "name": filename,
                "id": client.create_knowledge_document(filename, text),
                "usage_mode": declared.get("usage_mode", "prompt"),
            }
        )
    return attachments, superseded


def merged_platform_settings(declared: dict, live: dict) -> dict:
    """
    Lays the settings this repository declares over the ones the agent already has.

    declared: the platform_settings block from agent.json. A subset, deliberately.
    live:     what the agent currently carries.

    Returns: the merged settings.

    Merged rather than replaced because agent.json declares only what this project has an
    opinion about -- retention, and which fields a call may override. Sending the subset
    alone would drop the rest of a settings object that has some thirty keys in it, most of
    them nothing to do with us.
    """
    merged = dict(live)
    for key, value in declared.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


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
    parser.add_argument(
        "--agent-id",
        default=json.loads((ROOT / "agent" / "agent.json").read_text())["agent_id"],
        help="defaults to the id in agent.json",
    )
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
        declared = (agent_config.get("platform_settings") or {}).get("privacy", {})
        print(f"  retention_days -> {declared.get('retention_days')}")
        for filename in (agent_config.get("knowledge_base") or {}).get("documents") or []:
            size = len((ROOT / "agent" / "knowledge" / filename).read_text())
            print(f"  knowledge -> {filename} ({size} chars)")
        for tool in tools:
            print(f"  {tool['name']:22} {tool['api_schema']['url']}")
        return 0

    existing = client.existing_tools()
    tool_ids = [client.upsert_tool(tool, existing) for tool in tools]
    knowledge, superseded = publish_knowledge(client, agent_config.get("knowledge_base") or {})

    conversation_config = json.loads(json.dumps(agent_config["conversation_config"]))
    conversation_config["agent"]["prompt"]["prompt"] = prompt
    conversation_config["agent"]["prompt"]["tool_ids"] = tool_ids
    conversation_config["agent"]["prompt"]["knowledge_base"] = knowledge

    settings = merged_platform_settings(
        agent_config.get("platform_settings") or {},
        client.agent(args.agent_id).get("platform_settings") or {},
    )

    client.update_agent(
        args.agent_id,
        {
            "name": agent_config["name"],
            "conversation_config": conversation_config,
            "platform_settings": settings,
        },
    )
    for document_id in superseded:
        client.delete_knowledge_document(document_id)

    declared_webhook = agent_config.get("webhook") or {}
    for hook in client.workspace_webhooks() if declared_webhook else []:
        if hook.get("name") == declared_webhook.get("name"):
            client.update_webhook(
                hook["webhook_id"],
                declared_webhook["name"],
                bool(declared_webhook.get("retry_enabled")),
                bool(hook.get("is_disabled")),
            )
            state = "disabled" if hook.get("is_disabled") else "enabled"
            print(
                f"webhook {declared_webhook['name']}: retries "
                f"{'on' if declared_webhook.get('retry_enabled') else 'off'}, currently {state}"
            )

    print(f"synced {len(tool_ids)} tools and a {len(prompt)}-character prompt")
    print(f"knowledge: {len(knowledge)} document(s), {len(superseded)} replaced")
    print(f"retention set to {settings.get('privacy', {}).get('retention_days')} days")
    print(f"agent {args.agent_id} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())

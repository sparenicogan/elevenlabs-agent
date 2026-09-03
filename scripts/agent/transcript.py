"""Prints a call transcript with its tool calls.

The dashboard shows the same thing, but a terminal copy can be diffed, pasted into a commit
message, and read without leaving the work.
"""

import argparse
import json
import subprocess
import sys

import httpx

API = "https://api.elevenlabs.io/v1"


def secret(name: str) -> str:
    """Reads a value from Secrets Manager."""
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


def main() -> int:
    """
    Prints one conversation: outcome, then each turn with the tools it called.

    Returns: 0 on success. Defaults to the most recent conversation for the agent.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--conversation-id", help="defaults to the most recent call")
    parser.add_argument("--list", action="store_true", help="list recent calls instead")
    args = parser.parse_args()

    client = httpx.Client(
        headers={"xi-api-key": secret("voice-agent/elevenlabs/api-key")}, timeout=30
    )

    if args.list or not args.conversation_id:
        listing = client.get(
            f"{API}/convai/conversations",
            params={"agent_id": args.agent_id, "page_size": 10},
        ).json()["conversations"]
        if args.list:
            for c in listing:
                print(
                    f"{c['conversation_id']}  {c.get('status'):8}  {c.get('call_duration_secs')}s"
                )
            return 0
        if not listing:
            print("no conversations yet")
            return 1
        conversation_id = listing[0]["conversation_id"]
    else:
        conversation_id = args.conversation_id

    call = client.get(f"{API}/convai/conversations/{conversation_id}").json()
    meta = call.get("metadata", {})

    print(f"{conversation_id}")
    status = call.get("status")
    duration = meta.get("call_duration_secs")
    print(f"status {status} | {duration}s | {meta.get('termination_reason')}")
    if meta.get("error"):
        print(f"error: {json.dumps(meta['error'])}")
    print("=" * 78)

    for turn in call.get("transcript") or []:
        message = (turn.get("message") or "").strip()
        if message:
            print(f"\n[{turn.get('role', '?').upper()}] {message}")
        for tool_call in turn.get("tool_calls") or []:
            params = json.dumps(tool_call.get("params_as_json"))[:300]
            print(f"    -> {tool_call.get('tool_name')} {params}")
        for result in turn.get("tool_results") or []:
            value = result.get("result_value") or result.get("result")
            print(f"    <- {json.dumps(value)[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

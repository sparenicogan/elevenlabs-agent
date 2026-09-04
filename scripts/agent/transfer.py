"""Points the agent's escalation transfer at a phone number.

Separate from sync.py, and the number is passed in rather than stored, because a real phone
number does not belong in the repository — the secret scanner rejects one, and rightly.

    DEMO_TRANSFER_NUMBER=+41... uv run python -m scripts.agent.transfer --agent-id agent_...
"""

import argparse
import os

import httpx

from scripts.agent.sync import API, secret

TIMEOUT = 30.0

DESCRIPTION = (
    "Hand the caller to a person. Use it when identity cannot be established, the call is "
    "locked, a credit is above your authority, a tool keeps failing, or the caller asks for a "
    "human. Call create_escalation first so they arrive with context, and tell the caller "
    "about the callback before you transfer."
)

CONDITION = (
    "The caller needs a person: identity could not be established, the call is locked, the "
    "request is above the agent's authority, a tool keeps failing, or they asked for a human."
)


def main() -> int:
    """
    Sets the transfer destination on the agent.

    Returns: 0 on success. The number comes from DEMO_TRANSFER_NUMBER in E.164 form.

    Conference transfer: the agent stays on the line long enough to hand over, and a failed
    transfer leaves the caller with the agent rather than with silence.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", required=True)
    args = parser.parse_args()

    number = os.environ.get("DEMO_TRANSFER_NUMBER")
    if not number:
        raise SystemExit("set DEMO_TRANSFER_NUMBER to the destination, in E.164 form")

    client = httpx.Client(
        headers={"xi-api-key": secret("voice-agent/elevenlabs/api-key")}, timeout=TIMEOUT
    )
    response = client.patch(
        f"{API}/convai/agents/{args.agent_id}",
        json={
            "conversation_config": {
                "agent": {
                    "prompt": {
                        "built_in_tools": {
                            "transfer_to_number": {
                                "name": "transfer_to_number",
                                "description": DESCRIPTION,
                                "params": {
                                    "system_tool_type": "transfer_to_number",
                                    "transfers": [
                                        {
                                            "transfer_destination": {
                                                "type": "phone",
                                                "phone_number": number,
                                            },
                                            "condition": CONDITION,
                                            "transfer_type": "conference",
                                        }
                                    ],
                                },
                            }
                        }
                    }
                }
            }
        },
    )
    if response.status_code >= 400:
        raise SystemExit(f"{response.status_code}\n{response.text[:600]}")

    print(f"transfer set to ****{number[-4:]} on {args.agent_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

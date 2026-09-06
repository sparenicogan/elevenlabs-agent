"""Fills the performance table from ElevenLabs, and reports what is missing.

Two jobs, because they are the same query. Every completed call should have a row: the
post-call webhook writes one as the call ends. A call ElevenLabs knows about that we do not
means a delivery was lost, and until something compares the two lists nothing says so -- a
missing row looks exactly like a quiet week.

    AWS_PROFILE=voice-agent-admin uv run python -m scripts.backfill --days 30
    AWS_PROFILE=voice-agent-admin uv run python -m scripts.backfill --days 30 --write

Reads by default. Nothing is written without --write.
"""

import argparse
import json
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import boto3
import httpx

from src.domain.performance import build

ROOT = pathlib.Path(__file__).resolve().parents[1]
# The workspace holds more than one agent and the key reaches all of them. Without this
# filter a comparison counts somebody else's calls as gaps in ours.
AGENT_ID = json.loads((ROOT / "agent" / "agent.json").read_text())["agent_id"]
API = "https://api.elevenlabs.io/v1"
TABLE = "voice-agent-performance"
TIMEOUT = 60.0


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


def conversations(key: str, since: datetime) -> list[dict]:
    """
    Every conversation ElevenLabs holds that started after a given moment.

    key:   the ElevenLabs API key.
    since: the earliest call to consider.

    Returns: list rows, newest first. Paged through in full, because a partial answer here
             would report calls as missing that simply were not asked for.
    """
    found, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["cursor"] = cursor
        page = httpx.get(
            f"{API}/convai/conversations",
            headers={"xi-api-key": key},
            params=params,
            timeout=TIMEOUT,
        ).json()
        for row in page.get("conversations", []):
            started = datetime.fromtimestamp(row.get("start_time_unix_secs", 0), UTC)
            if started >= since and row.get("agent_id") == AGENT_ID:
                found.append(row)
        cursor = page.get("next_cursor")
        if not cursor or not page.get("conversations"):
            return found


def main() -> int:
    """
    Compares ElevenLabs against the performance table and optionally closes the gap.

    Returns: 0 when the two agree or the gap was filled, 1 when rows are missing and --write
             was not given, so a scheduled run fails loudly rather than reporting nothing.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="how far back to compare")
    parser.add_argument("--write", action="store_true", help="write the missing rows")
    args = parser.parse_args()

    key = secret("voice-agent/elevenlabs/api-key")
    table = boto3.resource("dynamodb").Table(TABLE)
    since = datetime.now(UTC) - timedelta(days=args.days)

    listed = conversations(key, since)
    print(f"{len(listed)} conversations for this agent in the last {args.days} days")

    missing = [
        row
        for row in listed
        if not table.get_item(Key={"conversation_id": row["conversation_id"]}).get("Item")
    ]
    if not missing:
        print("performance table is complete")
        return 0

    print(f"{len(missing)} missing from the performance table")
    if not args.write:
        for row in missing[:10]:
            started = datetime.fromtimestamp(row.get("start_time_unix_secs", 0), UTC)
            print(f"  {row['conversation_id']}  {started:%Y-%m-%d %H:%M}  {row.get('status')}")
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more")
        print("\nre-run with --write to fill them")
        return 1

    written = 0
    for row in missing:
        detail = httpx.get(
            f"{API}/convai/conversations/{row['conversation_id']}",
            headers={"xi-api-key": key},
            timeout=TIMEOUT,
        ).json()
        # No customer_id: the conversation record that held it may have expired, and a
        # backfilled row is about what the call cost and how it went, not who was on it.
        item = build(detail, customer_id="")
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(conversation_id)")
        written += 1
        print(f"  wrote {row['conversation_id']}  {item['outcome']:<20} {item['channel']}")

    print(f"\nwrote {written} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())

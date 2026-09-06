"""Prints the nine rates in FR-043 from the stored conversation records.

Reads and derives; writes nothing. Every rate is computed from records that already exist, so
none of them can drift from what actually happened — which is the point of not storing them.

    AWS_PROFILE=voice-agent-admin uv run python -m scripts.metrics --days 30
"""

import argparse
import json
from datetime import UTC, datetime, timedelta

import boto3

from src.domain.metrics import derive, from_record

TABLE = "voice-agent-performance"

# How each rate reads to somebody who has not read the specification.
LABELS = {
    "first_call_resolution": "First-call resolution",
    "autonomous_resolution_rate": "Resolved without a person",
    "escalation_rate": "Escalated",
    "average_handling_time_seconds": "Average handling time (s)",
    "tool_error_rate": "Tool error rate",
    "verification_failure_rate": "Verification failure rate",
    "credit_issuance_rate": "Calls requesting a credit",
    "blocked_credit_rate": "Credit requests refused",
    "containment_rate": "Contained without transfer",
}


def _recent(days: int) -> list[dict]:
    """
    Reads the conversations that started inside the window.

    days: how far back to look.

    Returns: the raw records. A scan rather than a query because the table is keyed by
             conversation and the question is about a period, not a customer — and at demo
             volumes a scan is one page.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    table = boto3.resource("dynamodb").Table(TABLE)

    records, start = [], None
    while True:
        page = table.scan(**({"ExclusiveStartKey": start} if start else {}))
        records += [r for r in page.get("Items", []) if str(r.get("started_at", "")) >= cutoff]
        start = page.get("LastEvaluatedKey")
        if not start:
            return records


def _medium(record: dict) -> str:
    """Whether somebody phoned in or used the web widget. Both are real interactions."""
    return "phone" if record.get("is_phone_call") else "web"


def _counts(records: list[dict]) -> dict[str, int]:
    """How many interactions arrived by each medium."""
    counts: dict[str, int] = {}
    for record in records:
        medium = _medium(record)
        counts[medium] = counts.get(medium, 0) + 1
    return counts


def _split(records: list[dict]) -> str:
    """The medium breakdown as a phrase, or nothing when they all came the same way."""
    counts = _counts(records)
    if len(counts) < 2:
        return ""
    return " (" + ", ".join(f"{n} {medium}" for medium, n in sorted(counts.items())) + ")"


def main() -> int:
    """
    Prints the rates for the period.

    Returns: 0 on success. --json prints the raw numbers for anything downstream; the default
             is a table, because the usual reader is a person deciding whether the agent is
             behaving.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    # Everything counts. The medium is reported rather than filtered on, because a web
    # session is a real interaction and leaving it out would describe a quieter, tidier
    # service than the one running.
    parser.add_argument(
        "--medium",
        choices=("phone", "web"),
        help="narrow to one medium; the default counts both",
    )
    args = parser.parse_args()

    records = _recent(args.days)
    if args.medium:
        records = [r for r in records if _medium(r) == args.medium]
    rates = derive([from_record(r) for r in records])

    if args.json:
        print(
            json.dumps(
                {
                    "days": args.days,
                    "interactions": len(records),
                    "by_medium": _counts(records),
                    **rates,
                },
                indent=2,
            )
        )
        return 0

    print(f"{len(records)} interactions in the last {args.days} days{_split(records)}\n")
    if not records:
        print("  nothing to derive from yet")
        return 0

    for name, value in rates.items():
        shown = f"{value:.0f}s" if name.endswith("_seconds") else f"{value:>6.1%}"
        print(f"  {LABELS[name]:<32} {shown:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Loads the synthetic fixtures into DynamoDB.

Idempotent: writes are unconditional puts on known keys, so re-running restores the
fixtures to their starting state after a demo run has moved them. That matters — the golden
path mutates a payment to UNDER_REVIEW, and the next rehearsal needs it back.
"""

import argparse
from decimal import Decimal

import boto3

from scripts.seed import fixtures

PROJECT = "voice-agent"


def _decimalise(value):
    """DynamoDB stores numbers as Decimal and rejects float. Amounts are strings in the
    fixtures precisely so they never pass through binary floating point."""
    if isinstance(value, dict):
        return {k: _decimalise(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_decimalise(v) for v in value]
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    return value


def _amounts_to_decimal(entry: dict) -> dict:
    """Converts the ledger's string amounts into the numeric type the table expects."""
    converted = dict(entry)
    if "amount" in converted:
        converted["amount"] = Decimal(str(converted["amount"]))
    return converted


def main() -> int:
    """
    Writes customers and ledger entries into the project's tables.

    Returns: 0 on success. Prints a count per table so a run is verifiable at a glance.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", help="unused for now; keeps the CLI stable")
    parser.parse_args()

    dynamodb = boto3.resource("dynamodb")
    identity = dynamodb.Table(f"{PROJECT}-customer-identity")
    ledger = dynamodb.Table(f"{PROJECT}-ledger")

    for customer in fixtures.CUSTOMERS:
        identity.put_item(Item=_decimalise(customer))

    for entry in fixtures.LEDGER:
        ledger.put_item(Item=_decimalise(_amounts_to_decimal(entry)))

    print(f"seeded {len(fixtures.CUSTOMERS)} customers into {identity.name}")
    print(f"seeded {len(fixtures.LEDGER)} ledger entries into {ledger.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

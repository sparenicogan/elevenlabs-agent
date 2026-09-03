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


def _purge_orphans(table, keys: set[tuple[str, ...]], key_fields: tuple[str, ...]) -> int:
    """
    Deletes rows the fixtures no longer describe.

    table:      the boto3 Table.
    keys:       the primary keys the fixtures define.
    key_fields: which attributes form the key.

    Returns: how many rows were removed. Overwriting alone is not enough — an entry removed
             from the fixtures would otherwise survive as a phantom invoice and quietly
             change a customer's balance.
    """
    removed = 0
    scan = table.scan(ProjectionExpression=", ".join(key_fields))
    for item in scan.get("Items", []):
        key = tuple(item[field] for field in key_fields)
        if key not in keys:
            table.delete_item(Key=dict(zip(key_fields, key, strict=True)))
            removed += 1
    return removed


def main() -> int:
    """
    Writes customers and ledger entries into the project's tables.

    Returns: 0 on success. Rows the fixtures no longer describe are deleted first, so
             seeding is a reset rather than an overlay and a demo always starts from a known
             state. Prints a count per table so a run is verifiable at a glance.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", help="unused for now; keeps the CLI stable")
    parser.parse_args()

    dynamodb = boto3.resource("dynamodb")
    identity = dynamodb.Table(f"{PROJECT}-customer-identity")
    ledger = dynamodb.Table(f"{PROJECT}-ledger")

    identity_keys = {(c["customer_id"],) for c in fixtures.CUSTOMERS}
    ledger_keys = {(e["customer_id"], e["entry_id"]) for e in fixtures.LEDGER}

    dropped_identity = _purge_orphans(identity, identity_keys, ("customer_id",))
    dropped_ledger = _purge_orphans(ledger, ledger_keys, ("customer_id", "entry_id"))

    for customer in fixtures.CUSTOMERS:
        identity.put_item(Item=_decimalise(customer))

    for entry in fixtures.LEDGER:
        ledger.put_item(Item=_decimalise(_amounts_to_decimal(entry)))

    print(
        f"seeded {len(fixtures.CUSTOMERS)} customers into {identity.name} "
        f"({dropped_identity} stale removed)"
    )
    print(
        f"seeded {len(fixtures.LEDGER)} ledger entries into {ledger.name} "
        f"({dropped_ledger} stale removed)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

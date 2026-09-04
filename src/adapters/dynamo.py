"""DynamoDB access. The only module that talks to the tables.

Reads retry once; writes never do (FR-023). Conditional writes are the mechanism behind
idempotency, so the failure of a condition is a normal outcome here, not an error.
"""

import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src.adapters.errors import ErrorCategory, ToolError

_PROJECT = os.environ.get("PROJECT", "voice-agent")
_READ_RETRIES = 1
_RETRY_BACKOFF_SECONDS = 0.2

_resource = None


def _table(name: str):
    """Returns a table resource, created once per cold start."""
    global _resource
    if _resource is None:
        _resource = boto3.resource("dynamodb")
    return _resource.Table(f"{_PROJECT}-{name}")


def get(table: str, key: dict[str, Any]) -> dict | None:
    """
    Reads one item.

    table: logical table name without the project prefix, e.g. "ledger".
    key:   the full primary key.

    Returns: the item, or None when it does not exist. Raises ToolError(DEPENDENCY_DOWN)
             when the table cannot be reached, which the caller must not treat as absence.
    """
    for attempt in range(_READ_RETRIES + 1):
        try:
            return _table(table).get_item(Key=key).get("Item")
        except ClientError as exc:
            if attempt == _READ_RETRIES:
                raise ToolError(ErrorCategory.DEPENDENCY_DOWN, f"get {table}: {exc}") from exc
            time.sleep(_RETRY_BACKOFF_SECONDS)
    return None


def query(table: str, index: str | None = None, **kwargs: Any) -> list[dict]:
    """
    Runs a query, optionally against a secondary index.

    table:  logical table name without the project prefix.
    index:  GSI name, or None for the base table.
    kwargs: passed through to boto3, e.g. KeyConditionExpression.

    Returns: the matching items, empty when there are none. Raises
             ToolError(DEPENDENCY_DOWN) on failure — an empty list means "none exist",
             never "could not tell".
    """
    if index:
        kwargs["IndexName"] = index

    for attempt in range(_READ_RETRIES + 1):
        try:
            return _table(table).query(**kwargs).get("Items", [])
        except ClientError as exc:
            if attempt == _READ_RETRIES:
                raise ToolError(ErrorCategory.DEPENDENCY_DOWN, f"query {table}: {exc}") from exc
            time.sleep(_RETRY_BACKOFF_SECONDS)
    return []


def put_if_absent(table: str, item: dict[str, Any], key_field: str) -> bool:
    """
    Writes an item only if it does not already exist. The idempotency primitive.

    table:     logical table name without the project prefix.
    item:      the complete item to write.
    key_field: the partition key attribute name, used to build the condition.

    Returns: True when written, False when an item already existed — which is a duplicate
             request, not a failure. Never retried: a write that may have partly succeeded
             must not be repeated (FR-023).
    """
    try:
        _table(table).put_item(
            Item=item,
            ConditionExpression=f"attribute_not_exists({key_field})",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise ToolError(ErrorCategory.DEPENDENCY_DOWN, f"put {table}: {exc}") from exc


def update_if(table: str, key: dict[str, Any], condition: str, **kwargs: Any) -> dict | None:
    """
    Updates an item only while a condition holds. Guards state transitions.

    table:     logical table name without the project prefix.
    key:       the full primary key.
    condition: a ConditionExpression, e.g. "#status = :unallocated", which makes an
               illegal transition impossible even under concurrent calls.
    kwargs:    passed through to boto3, e.g. UpdateExpression and attribute maps.

    Returns: the updated attributes, or None when the condition did not hold — meaning the
             record had already moved on, typically a duplicate request.
    """
    try:
        response = _table(table).update_item(
            Key=key,
            ConditionExpression=condition,
            ReturnValues="ALL_NEW",
            **kwargs,
        )
        return response.get("Attributes")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise ToolError(ErrorCategory.DEPENDENCY_DOWN, f"update {table}: {exc}") from exc


def upsert(table: str, key: dict[str, Any], **kwargs: Any) -> dict | None:
    """
    Updates an item, creating it if it does not exist.

    table:  logical table name without the project prefix.
    key:    the full primary key.
    kwargs: passed through to boto3, e.g. UpdateExpression and attribute maps.

    Returns: the updated attributes.

    Distinct from update_if, which enforces a condition and returns None when it fails. Use
    this for state that ought to exist by the time it is written but whose absence must not
    silently discard the write — verification status being the case that matters, since
    discarding it looks identical to the disclosure gate working.
    """
    if "condition" in kwargs:
        # Caught here rather than by boto three frames down, where it reads as an unknown
        # parameter and says nothing about which function was wanted. This cost a 500 on a
        # deployed webhook that every offline test passed, because the adapter was mocked.
        raise TypeError("upsert takes no condition; use update_if for a conditional write")

    try:
        response = _table(table).update_item(Key=key, ReturnValues="ALL_NEW", **kwargs)
        return response.get("Attributes")
    except ClientError as exc:
        raise ToolError(ErrorCategory.DEPENDENCY_DOWN, f"upsert {table}: {exc}") from exc

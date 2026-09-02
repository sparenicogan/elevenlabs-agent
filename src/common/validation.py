"""Server-side validation of tool inputs.

FR-046 requires this regardless of what the agent claims to have sent. The agent is a
language model: a field can arrive absent, as the wrong type, or as a spoken phrase where
a number was expected.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from src.adapters.errors import ErrorCategory, ToolError


def require(payload: dict, field: str) -> object:
    """
    Reads a required field, or fails the call.

    payload: the parsed request body.
    field:   the key that must be present and non-empty.

    Returns: the field's value. Raises ToolError(VALIDATION) when missing.
    """
    value = payload.get(field)
    if value is None or value == "":
        raise ToolError(ErrorCategory.VALIDATION, f"missing field: {field}")
    return value


def money(payload: dict, field: str) -> Decimal:
    """
    Reads a monetary amount as an exact decimal.

    payload: the parsed request body.
    field:   key holding an amount, as a number or numeric string.

    Returns: Decimal in the ledger's currency units. Floats are never used for money —
             4200.00 is not representable in binary and exact matching depends on it
             (FR-010a).
    """
    try:
        return Decimal(str(require(payload, field)))
    except (InvalidOperation, ValueError) as exc:
        raise ToolError(ErrorCategory.VALIDATION, f"unreadable amount: {field}") from exc


def iso_date(payload: dict, field: str) -> date:
    """
    Reads a calendar date.

    payload: the parsed request body.
    field:   key holding an ISO-8601 date, e.g. "2026-07-02".

    Returns: a date. Raises ToolError(VALIDATION) on anything else, including a spoken
             phrase like "last Tuesday", which must be resolved by the agent before the
             tool is called.
    """
    try:
        return date.fromisoformat(str(require(payload, field)))
    except ValueError as exc:
        raise ToolError(ErrorCategory.VALIDATION, f"unreadable date: {field}") from exc

"""Per-conversation verification state, held server-side.

Principle I turns on this module. If verification status lived in the prompt, a caller who
talked their way past the model would be verified. Here, every financial tool asks the
conversations table, and the model's opinion is irrelevant.
"""

from datetime import UTC, datetime
from enum import StrEnum

from src.adapters import dynamo
from src.adapters.errors import ErrorCategory, ToolError

_TABLE = "conversations"


class VerificationStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    LOCKED = "LOCKED"


def start(conversation_id: str, agent_id: str, agent_version: str, language: str) -> None:
    """
    Creates the conversation record at the start of a call.

    conversation_id: from the initiation webhook. Also the post-call idempotency key.
    agent_id:        which agent answered.
    agent_version:   so behaviour can be attributed after a change (FR-041).
    language:        the greeting language.

    Returns: nothing. A duplicate initiation leaves the existing record untouched.
    """
    dynamo.put_if_absent(
        _TABLE,
        {
            "conversation_id": conversation_id,
            "agent_id": agent_id,
            "agent_version": agent_version,
            "language": language,
            "started_at": datetime.now(UTC).isoformat(),
            "verification_status": VerificationStatus.UNVERIFIED,
        },
        key_field="conversation_id",
    )


def set_verification(
    conversation_id: str,
    status: VerificationStatus,
    customer_id: str,
    display: dict[str, str] | None = None,
) -> None:
    """
    Records the outcome of a verification attempt.

    conversation_id: the call.
    status:          the backend's decision, never the model's.
    customer_id:     the customer resolved from the factors themselves (research D3).
    display:         the few non-sensitive fields later tools need — company name, language,
                     account status, HubSpot ids. Carried here so that reading them does not
                     require a third handler to hold permission on the identity table
                     (Principle IV).

    Returns: nothing.
    """
    # Creates the record when the call never passed through the initiation webhook. A
    # conditional update silently does nothing there, and a verified caller is then treated
    # as unverified for the rest of the call — a failure that looks exactly like the gate
    # working correctly.
    dynamo.upsert(
        _TABLE,
        {"conversation_id": conversation_id},
        UpdateExpression=(
            "SET verification_status = :s, customer_id = :c, "
            "started_at = if_not_exists(started_at, :now), customer_display = :d"
        ),
        ExpressionAttributeValues={
            ":s": str(status),
            ":c": customer_id,
            ":now": datetime.now(UTC).isoformat(),
            ":d": display or {},
        },
    )


def record_failed_attempt(conversation_id: str) -> int:
    """
    Counts a failed verification attempt against the call itself.

    conversation_id: the call in progress.

    Returns: how many attempts have now failed in this call.

    Per-customer counting alone leaves a hole: a caller who never names an account cannot
    have their failures attributed to one, and could guess indefinitely. FR-006 requires
    counting per caller session as well, and this is that half.
    """
    updated = dynamo.upsert(
        _TABLE,
        {"conversation_id": conversation_id},
        UpdateExpression="ADD failed_verification_attempts :one",
        ExpressionAttributeValues={":one": 1},
    )
    return int(updated.get("failed_verification_attempts", 1)) if updated else 1


def verified_context(conversation_id: str) -> tuple[str, dict]:
    """
    The gate, plus the display fields verification recorded.

    conversation_id: the call.

    Returns: (customer id, display fields). Raises ToolError(NOT_AUTHORIZED) unless the
             stored status is VERIFIED. Lets a downstream tool identify the customer without
             any permission on the identity table.
    """
    record = dynamo.get(_TABLE, {"conversation_id": conversation_id})

    if not record or record.get("verification_status") != VerificationStatus.VERIFIED:
        raise ToolError(ErrorCategory.NOT_AUTHORIZED, "conversation is not verified")

    return record["customer_id"], dict(record.get("customer_display") or {})


def require_verified(conversation_id: str) -> str:
    """
    Gate for every tool that touches financial data.

    conversation_id: the call.

    Returns: the verified customer id. Raises ToolError(NOT_AUTHORIZED) unless the stored
             status is VERIFIED — PARTIALLY_VERIFIED permits nothing financial (FR-001,
             US2 scenario 4).
    """
    record = dynamo.get(_TABLE, {"conversation_id": conversation_id})

    if not record or record.get("verification_status") != VerificationStatus.VERIFIED:
        raise ToolError(ErrorCategory.NOT_AUTHORIZED, "conversation is not verified")

    return record["customer_id"]

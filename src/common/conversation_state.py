"""Per-conversation verification state, held server-side.

Principle I turns on this module. If verification status lived in the prompt, a caller who
talked their way past the model would be verified. Here, every financial tool asks the
conversations table, and the model's opinion is irrelevant.
"""

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from boto3.dynamodb.conditions import Key

from src.adapters import dynamo
from src.adapters.errors import ErrorCategory, ToolError
from src.domain.risk import RiskSignal

_TABLE = "conversations"


# How long a conversation record survives. Deliberately longer than the 365-day window the
# credit risk rules look back over, because those rules still read this table. It drops to
# days once the performance table serves that history: verification outcomes, lockout
# counters and fingerprints of what a caller guessed are useful for minutes and a liability
# for months.
STATE_RETENTION_DAYS = 400

# Appended to every write that might be the one creating the row. There is no single place a
# conversation is opened -- nothing calls start(), and the record appears when whichever tool
# runs first writes to it -- so the row's birthday and its expiry are set by all of them,
# whichever gets there first.
_TOUCH = (
    "started_at = if_not_exists(started_at, :now), expires_at = if_not_exists(expires_at, :expires)"
)


def _expires_at() -> int:
    """The Unix second DynamoDB should drop the row on. TTL reads epoch seconds, nothing else."""
    return int((datetime.now(UTC) + timedelta(days=STATE_RETENTION_DAYS)).timestamp())


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
            "SET verification_status = :s, customer_id = :c, " + _TOUCH + ", customer_display = :d"
        ),
        ExpressionAttributeValues={
            ":s": str(status),
            ":c": customer_id,
            ":now": datetime.now(UTC).isoformat(),
            ":expires": _expires_at(),
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


def record_risk_signal(signal: RiskSignal) -> None:
    """
    Appends a risk signal to the conversation it was observed on.

    signal: what was observed. Its evidence must already be free of caller-supplied and
            stored values — this function does not sanitise, it stores.

    Returns: nothing.

    Signals live on the conversation rather than in their own table because every one is
    raised during a call and read back only as an aggregate over a customer's recent calls,
    which the customer-index on this table already serves.
    """
    dynamo.upsert(
        _TABLE,
        {"conversation_id": signal.conversation_id},
        UpdateExpression=(
            "SET risk_signals = list_append(if_not_exists(risk_signals, :empty), :signal), "
            + _TOUCH
        ),
        ExpressionAttributeValues={
            ":empty": [],
            ":signal": [
                {
                    "signal_type": str(signal.signal_type),
                    "evidence": signal.evidence,
                    "customer_id": signal.customer_id,
                    "observed_at": signal.observed_at,
                }
            ],
            ":now": datetime.now(UTC).isoformat(),
            ":expires": _expires_at(),
        },
    )


def risk_signals(conversation_id: str) -> list[RiskSignal]:
    """
    Reads back the signals raised during this call.

    conversation_id: the call.

    Returns: the signals, oldest first. Empty when none were raised — and a failure raises
             rather than returning empty, because "no signals" and "could not check" must
             not be the same answer to a risk question.
    """
    record = dynamo.get(_TABLE, {"conversation_id": conversation_id}) or {}
    return [
        RiskSignal(
            signal_type=entry["signal_type"],
            evidence=entry.get("evidence", ""),
            conversation_id=conversation_id,
            customer_id=entry.get("customer_id"),
            observed_at=entry.get("observed_at", ""),
        )
        for entry in record.get("risk_signals", [])
    ]


def record_factor_attempts(
    conversation_id: str, fingerprints: dict[str, str]
) -> tuple[dict[str, int], bool]:
    """
    Records which answers have been offered for which fields.

    conversation_id: the call.
    fingerprints:    field name to fingerprint of the value offered, from
                     verification.fingerprint. Never the values themselves.

    Returns: (field name to distinct-attempt count, whether this call offered anything new).

    A set per field rather than a counter, because the agent resends every factor gathered so
    far on each call: counting increments would treat one caller repeating themselves as
    dozens of attempts, and lock out everyone.

    The second return value exists for the same reason at a different layer. A caller who
    mistyped one answer has it resent on every subsequent turn, and counting each resend as a
    fresh failure locks them out three turns after a single typo — however correct everything
    they say afterwards is.
    """
    record = dynamo.get(_TABLE, {"conversation_id": conversation_id}) or {}
    seen: dict[str, list[str]] = dict(record.get("factor_attempts") or {})
    offered_something_new = False

    for field, value in fingerprints.items():
        existing = list(seen.get(field, []))
        if value not in existing:
            existing.append(value)
            offered_something_new = True
        seen[field] = existing

    dynamo.upsert(
        _TABLE,
        {"conversation_id": conversation_id},
        UpdateExpression=("SET factor_attempts = :attempts, " + _TOUCH),
        ExpressionAttributeValues={
            ":attempts": seen,
            ":now": datetime.now(UTC).isoformat(),
            ":expires": _expires_at(),
        },
    )

    return {field: len(values) for field, values in seen.items()}, offered_something_new


def record_wrong_values(conversation_id: str, fingerprints: dict[str, str]) -> int:
    """
    Records the distinct wrong values a caller has offered, and returns the worst field.

    conversation_id: the call.
    fingerprints:    field name to the fingerprint of the value that did not match, from
                     verification.fingerprint. Never the values themselves.

    Returns: the largest number of distinct wrong values offered for any one field.

    Distinct values rather than failed calls, and this is the difference between a fair
    lockout and a hostile one. The agent resends every factor it has gathered, so one
    mistyped email arrives on every subsequent turn. Counting calls exhausts a three-strike
    allowance three turns after a single typo, however correct everything the caller says
    afterwards is.

    Per field, and worst-field rather than total, for the same reason. A caller who gives
    their email, phone and date of birth in one breath and cannot be found offers three wrong
    values at once — that is one failed attempt, not three, and counting it as three locked
    an honest caller on his opening sentence during the first voice test. Someone working
    through three different emails is the case this exists to stop, and that still trips it.
    """
    record = dynamo.get(_TABLE, {"conversation_id": conversation_id}) or {}
    seen = {field: set(values) for field, values in (record.get("wrong_values") or {}).items()}

    if fingerprints:
        for field, value in fingerprints.items():
            seen.setdefault(field, set()).add(value)

        dynamo.upsert(
            _TABLE,
            {"conversation_id": conversation_id},
            UpdateExpression=("SET wrong_values = :wrong, " + _TOUCH),
            ExpressionAttributeValues={
                ":wrong": {field: sorted(values) for field, values in seen.items()},
                ":now": datetime.now(UTC).isoformat(),
                ":expires": _expires_at(),
            },
        )

    return max((len(values) for values in seen.values()), default=0)


def recent_conversations(customer_id: str, limit: int = 50) -> list[dict]:
    """
    Reads a customer's recent calls, newest first.

    customer_id: the verified customer.
    limit:       how many to read. Fifty is far more than any pattern needs and small enough
                 to stay a single query.

    Returns: conversation records carrying started_at and outcome. Empty when the customer
             has never called before, which is the ordinary case for a new customer and must
             not look like a failure.
    """
    return dynamo.query(
        _TABLE,
        index="customer-index",
        KeyConditionExpression=Key("customer_id").eq(customer_id),
        ScanIndexForward=False,
        Limit=limit,
    )


def record_callback(
    conversation_id: str, customer_id: str | None, reason: str, ticket_id: str | None
) -> None:
    """
    Records that a caller is owed a call back.

    conversation_id: the call.
    customer_id:     the account, or None when the caller could not be identified — they are
                     owed a callback either way, and the person taking it will establish who
                     they were.
    reason:          why the call escalated.
    ticket_id:       the ticket carrying the context, where one was created.

    Returns: nothing.

    Kept on the conversation rather than in a queue of its own. A callback is a fact about a
    call that did not finish, and the record that says so is the one that already holds the
    transcript reference, the risk signals and the verification state — everything the person
    ringing back would want.
    """
    dynamo.upsert(
        _TABLE,
        {"conversation_id": conversation_id},
        UpdateExpression=(
            "SET callback_required = :yes, callback_reason = :reason, "
            "callback_ticket_id = :ticket, callback_customer_id = :customer, " + _TOUCH
        ),
        ExpressionAttributeValues={
            ":yes": True,
            ":reason": reason,
            ":ticket": ticket_id or "",
            ":customer": customer_id or "",
            ":now": datetime.now(UTC).isoformat(),
            ":expires": _expires_at(),
        },
    )


def set_resolved_contact(conversation_id: str, contact_id: str) -> None:
    """
    Remembers whose record the rest of this call's answers are checked against.

    conversation_id: the call.
    contact_id:      the contact an identifier resolved to.

    Returns: nothing.

    Resolving is not verifying, and this grants nothing on its own: a date of birth cannot be
    looked up, so checking one needs a record already in hand, and that is all this is for.
    """
    dynamo.upsert(
        _TABLE,
        {"conversation_id": conversation_id},
        UpdateExpression=("SET resolved_contact_id = :contact, " + _TOUCH),
        ExpressionAttributeValues={
            ":contact": contact_id,
            ":now": datetime.now(UTC).isoformat(),
            ":expires": _expires_at(),
        },
    )


def resolved_contact(conversation_id: str) -> str | None:
    """Reads the contact an earlier answer in this call resolved to, or None."""
    record = dynamo.get(_TABLE, {"conversation_id": conversation_id}) or {}
    value = record.get("resolved_contact_id")
    return str(value) if value else None


def claim_post_call(conversation_id: str) -> bool:
    """
    Takes ownership of post-call processing for one conversation.

    conversation_id: the call that has ended.

    Returns: True the first time, False for every delivery after it.

    A conditional write rather than a read-then-write: two deliveries arriving together would
    both read "not processed" and both proceed, which is how a customer gets two summaries and
    two callbacks from one call (FR-024).
    """
    claimed = dynamo.update_if(
        _TABLE,
        {"conversation_id": conversation_id},
        condition="attribute_not_exists(post_call_processed_at)",
        UpdateExpression="SET post_call_processed_at = :now",
        ExpressionAttributeValues={":now": datetime.now(UTC).isoformat()},
    )
    return claimed is not None

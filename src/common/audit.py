"""Audit events for every consequential automated action.

FR-041 fixes the field set, so it is built from a dataclass rather than assembled at each
call site: an event missing its authorizing rule or its previous state cannot be
reconstructed later, and by then the call is over.

Events go to a dedicated log group with ten-year retention (FR-038a), not to the function's
own log group. That distinction is the whole point — a function's logs are operational and
expire in 90 days, while an audit record has to outlive the argument it might one day
settle.
"""

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

from src.common import logging as structured_log

_PROJECT = os.environ.get("PROJECT", "voice-agent")
AUDIT_LOG_GROUP = f"/{_PROJECT}/audit"

_client = None
_stream_name: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    """
    One consequential action, in a form that can be replayed and defended a decade later.

    action:          what was done, e.g. "propose_allocation" or "issue_credit".
    previous_state:  the record's state before the action. "NONE" when it did not exist.
    new_state:       the record's state after.
    authorizing_rule: the named rule that permitted it, e.g. "credit_max_per_request".
    customer_id:     the customer the action applies to.
    conversation_id: the call during which it happened.
    agent_version:   which agent version was live, so behaviour can be attributed after a
                     change.
    risk_result:     the risk evaluation outcome at decision time.
    human_approval_required: whether the action still needs a person to confirm it.
    entry_id:        the ledger entry acted on, where there is one.
    ticket_id:       the ticket raised, where there is one.
    """

    action: str
    previous_state: str
    new_state: str
    authorizing_rule: str
    customer_id: str
    conversation_id: str
    agent_version: str
    risk_result: str
    human_approval_required: bool
    entry_id: str | None = None
    ticket_id: str | None = None


def _stream() -> str:
    """
    Returns this execution environment's log stream, creating it once.

    One stream per function per day keeps streams readable while avoiding the contention of
    a single shared stream. The name includes the Lambda's log stream id, which is unique per
    execution environment, so two concurrent invocations never write to the same stream.
    """
    global _stream_name
    if _stream_name:
        return _stream_name

    function = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "local")
    unique = os.environ.get("AWS_LAMBDA_LOG_STREAM_NAME", str(time.time())).split("]")[-1]
    _stream_name = f"{datetime.now(UTC):%Y/%m/%d}/{function}/{unique}"

    try:
        _logs().create_log_stream(logGroupName=AUDIT_LOG_GROUP, logStreamName=_stream_name)
    except ClientError as exc:
        # Already exists is the normal case on a warm environment.
        if exc.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

    return _stream_name


def _logs():
    """The CloudWatch Logs client, created once per execution environment."""
    global _client
    if _client is None:
        _client = boto3.client("logs")
    return _client


def write(event: AuditEvent) -> None:
    """
    Records one audit event to the audit log group.

    event: a fully populated AuditEvent. Every field is required by FR-041; there is no
           partial audit.

    Returns: nothing.

    A failure to audit is logged but never raised. The action has already happened by the
    time this is called, and undoing a financial mutation because its record failed to write
    would turn a bookkeeping problem into a financial one. The failure is loud in the
    operational logs so it cannot pass unnoticed.
    """
    payload = asdict(event)
    payload["timestamp"] = datetime.now(UTC).isoformat()
    payload["event_type"] = "AUDIT"

    try:
        _logs().put_log_events(
            logGroupName=AUDIT_LOG_GROUP,
            logStreamName=_stream(),
            logEvents=[
                {"timestamp": int(time.time() * 1000), "message": json.dumps(payload, default=str)}
            ],
        )
    except ClientError as exc:
        structured_log.error(
            "AUDIT WRITE FAILED",
            action=event.action,
            conversation_id=event.conversation_id,
            entry_id=event.entry_id,
            error_detail=str(exc),
        )

"""The post-call webhook.

Four things happen after a call, in this order and independently: the transcript is stored,
the metrics are written, the customer summary is regenerated, and a failed transfer is
reconciled. Each step is allowed to fail without discarding the ones before it — a call whose
summary could not be written should still have its transcript and its metrics (FR-025).

The last step is a backstop rather than a feature. A transfer that drops the call takes the
agent's promise of a callback with it, and this is the only place left that knows one was
made (research D4).
"""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

from src.adapters import dynamo, secrets
from src.adapters.errors import ErrorCategory, ToolError
from src.common import call_history, conversation_state
from src.common import logging as log
from src.domain import locale
from src.domain import performance as performance_domain
from src.domain import policy as policy_module
from src.domain import summary as summary_domain

CONVERSATIONS_TABLE = "conversations"
PERFORMANCE_TABLE = "performance"
IDENTITY_TABLE = "customer-identity"
SUMMARIES_TABLE = "customer-summaries"

# How far out of date a signed request may be. Long enough for a slow delivery, short enough
# that a captured request is not replayable tomorrow.
SIGNATURE_WINDOW_SECONDS = 1800


def handler(event: dict, _context: Any = None) -> dict:
    """
    Records what happened on a call that has ended.

    event: API Gateway proxy event carrying the ElevenLabs post-call payload and its
           ElevenLabs-Signature header.

    Returns: 200 once the payload is accepted, whatever the individual steps did. A webhook
             that returns an error invites a redelivery, and every step here is idempotent or
             conditional, so a redelivery repeats work rather than correcting anything.
    """
    body = event.get("body") or ""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    try:
        _verify_signature(body, headers.get("elevenlabs-signature", ""))
    except ToolError as error:
        log.error(
            "post_call rejected", error_category=str(error.category), error_detail=error.detail
        )
        return {"statusCode": 401, "body": json.dumps({"status": "REJECTED"})}

    payload = json.loads(body or "{}")
    conversation_id = str(payload.get("conversation_id") or "")
    if not conversation_id:
        return {"statusCode": 400, "body": json.dumps({"status": "NO_CONVERSATION_ID"})}

    if not conversation_state.claim_post_call(conversation_id):
        # Already processed. Returning 200 rather than an error because a duplicate delivery
        # is not a fault, and an error would invite another one (FR-024).
        log.info("post_call already processed", conversation_id=conversation_id, status="DUPLICATE")
        return {"statusCode": 200, "body": json.dumps({"status": "DUPLICATE"})}

    steps = {
        "performance": lambda: _store_performance(conversation_id, payload),
        "history": lambda: _record_history(conversation_id, payload),
        "metrics": lambda: _store_metrics(conversation_id, payload),
        "summary": lambda: _regenerate_summary(conversation_id, payload),
        "preferences": lambda: _persist_preferences(payload),
        "transfer": lambda: _reconcile_transfer(conversation_id, payload),
    }

    completed = []
    for name, step in steps.items():
        try:
            step()
            completed.append(name)
        except (ToolError, KeyError, ValueError) as error:
            # Logged and carried on. Losing the summary must not cost us the transcript.
            log.error("post_call step failed", error_detail=f"{name}: {error}", status="DEGRADED")

    log.info("post_call processed", conversation_id=conversation_id, status="OK")
    return {"statusCode": 200, "body": json.dumps({"status": "OK", "completed": completed})}


def _verify_signature(body: str, header: str) -> None:
    """
    Checks the request came from ElevenLabs and is recent.

    body:   the raw request body, before parsing. Signed as text, so it must not be
            round-tripped through a parser first.
    header: the ElevenLabs-Signature header, "t=<unix>,v0=<hex>".

    Returns: nothing. Raises NOT_AUTHORIZED when the digest or the timestamp fails.

    The timestamp is checked before the digest and both are required. A valid signature on a
    request captured an hour ago is still a replay.
    """
    parts = dict(piece.split("=", 1) for piece in header.split(",") if "=" in piece)
    timestamp, digest = parts.get("t", ""), parts.get("v0", "")
    if not timestamp or not digest:
        raise ToolError(ErrorCategory.NOT_AUTHORIZED, "malformed signature header")

    try:
        age = abs(time.time() - int(timestamp))
    except ValueError as error:
        raise ToolError(ErrorCategory.NOT_AUTHORIZED, "unparseable timestamp") from error

    if age > SIGNATURE_WINDOW_SECONDS:
        raise ToolError(ErrorCategory.NOT_AUTHORIZED, f"timestamp {age:.0f}s outside the window")

    secret = secrets.get("elevenlabs/webhook-secret")
    expected = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()

    # Constant time, so a wrong signature cannot be improved one character at a time.
    if not hmac.compare_digest(expected, digest):
        raise ToolError(ErrorCategory.NOT_AUTHORIZED, "signature mismatch")


def _store_performance(conversation_id: str, payload: dict) -> None:
    """
    Writes the permanent record of the call.

    conversation_id: the call.
    payload:         the post-call payload.

    Returns: nothing. Written once and never updated -- a duplicate delivery finds the row
             already there and changes nothing, which is what put_if_absent is for. The row
             outlives the conversation record it is derived from, and no reset removes it.
    """
    record = dynamo.get(CONVERSATIONS_TABLE, {"conversation_id": conversation_id}) or {}
    row = performance_domain.build(payload, customer_id=str(record.get("customer_id") or ""))
    written = dynamo.put_if_absent(PERFORMANCE_TABLE, row, "conversation_id")
    log.info(
        "performance recorded" if written else "performance already recorded",
        conversation_id=conversation_id,
        customer_id=str(record.get("customer_id") or ""),
        outcome=row["outcome"],
        latency_ms=max((t["latency_max_ms"] for t in row["tools"].values()), default=0),
    )


def _record_history(conversation_id: str, payload: dict) -> None:
    """
    Notes the call against the customer, for the rules that decide about money.

    conversation_id: the call.
    payload:         the post-call payload.

    Returns: nothing. A call nobody verified belongs to no customer and is skipped -- there is
             no history to attach it to.
    """
    record = dynamo.get(CONVERSATIONS_TABLE, {"conversation_id": conversation_id}) or {}
    customer_id = str(record.get("customer_id") or "")
    row = performance_domain.build(payload, customer_id=customer_id)
    call_history.record(
        customer_id=customer_id,
        conversation_id=conversation_id,
        started_at=row["started_at"],
        outcome=row["outcome"],
    )


def _store_metrics(conversation_id: str, payload: dict) -> None:
    """
    Writes the per-interaction metrics in FR-042.

    conversation_id: the call.
    payload:         the post-call payload.

    Returns: nothing. Stores what happened and never a derived rate: a rate is computed from
             the set when it is asked for, so it cannot be a number that was true once.
    """
    analysis = payload.get("analysis") or {}
    metadata = payload.get("metadata") or {}

    dynamo.upsert(
        CONVERSATIONS_TABLE,
        {"conversation_id": conversation_id},
        UpdateExpression=(
            "SET ended_at = :ended, duration_seconds = :duration, #lang = :language, "
            "tools_invoked = :tools, outcome = :outcome, transfer = :transfer, "
            "agent_version = if_not_exists(agent_version, :version)"
        ),
        ExpressionAttributeNames={"#lang": "language"},
        ExpressionAttributeValues={
            ":ended": datetime.now(UTC).isoformat(),
            ":duration": int(metadata.get("call_duration_secs") or 0),
            ":language": str(metadata.get("language") or "en"),
            ":tools": _tool_counts(payload),
            ":outcome": _outcome(payload),
            ":transfer": {
                "attempted": bool(analysis.get("transfer_attempted")),
                "result": str(analysis.get("transfer_result") or "NONE"),
            },
            ":version": str(payload.get("agent_version") or "unknown"),
        },
    )


def _tool_counts(payload: dict) -> dict:
    """
    Counts what each tool did on the call.

    payload: the post-call payload, whose transcript carries the tool calls.

    Returns: tool name to {count, failures}, which is what the error rate is derived from.
    """
    counts: dict[str, dict[str, int]] = {}
    for turn in payload.get("transcript") or []:
        for call in turn.get("tool_calls") or []:
            name = str(call.get("tool_name") or "unknown")
            entry = counts.setdefault(name, {"count": 0, "failures": 0})
            entry["count"] += 1
        for result in turn.get("tool_results") or []:
            name = str(result.get("tool_name") or "unknown")
            entry = counts.setdefault(name, {"count": 0, "failures": 0})
            if result.get("is_error") or "SERVICE_UNAVAILABLE" in json.dumps(
                result.get("result_value") or ""
            ):
                entry["failures"] += 1
    return counts


def _outcome(payload: dict) -> str:
    """Which of the five outcomes in data-model.md this call had. See interaction.outcome:
    the rule reads fields ElevenLabs actually sends, which the previous one did not."""
    return performance_domain.outcome(payload)


def _regenerate_summary(conversation_id: str, payload: dict) -> None:
    """
    Folds this call into what is remembered about the customer.

    conversation_id: the call.
    payload:         the post-call payload.

    Returns: nothing. Skipped entirely when nobody was verified — an unidentified caller has
             no account to remember anything against.

    Written with a version check, so two calls ending at once cannot overwrite each other's
    work: the loser reads again and re-folds rather than winning by arriving second.
    """
    customer_id = payload.get("customer_id")
    if not customer_id:
        return

    settings = policy_module.load()
    existing = dynamo.get(SUMMARIES_TABLE, {"customer_id": str(customer_id)}) or {}
    version = int(existing.get("version", 0))

    text = summary_domain.regenerate(
        previous=str(existing.get("summary_text", "")),
        this_call=str((payload.get("analysis") or {}).get("transcript_summary") or ""),
        max_chars=settings.summary_max_chars,
    )
    if not text:
        return

    sources = [str(c) for c in (existing.get("source_conversation_ids") or [])]
    dynamo.update_if(
        SUMMARIES_TABLE,
        {"customer_id": str(customer_id)},
        condition="attribute_not_exists(version) OR version = :expected",
        UpdateExpression=(
            "SET summary_text = :text, updated_at = :now, version = :next, "
            "source_conversation_ids = :sources"
        ),
        ExpressionAttributeValues={
            ":expected": version,
            ":next": version + 1,
            ":text": text,
            ":now": datetime.now(UTC).isoformat(),
            ":sources": ([conversation_id] + sources)[:20],
        },
    )


def _persist_preferences(payload: dict) -> None:
    """
    Remembers the language a call actually happened in.

    payload: the post-call payload.

    Returns: nothing. Skipped when nobody was verified, and skipped when the language did not
             change — a write on every call would rewrite the same value all day.

    Stored against the contact rather than the company: two people at one customer may prefer
    different languages, and greeting the second in the first one's is the kind of small
    wrongness that makes an agent feel automated.
    """
    contact_id = payload.get("contact_id")
    if not contact_id:
        return

    language = locale.normalise((payload.get("metadata") or {}).get("language"))
    record = dynamo.get(IDENTITY_TABLE, {"contact_id": str(contact_id)}) or {}
    if record.get("preferred_language") == language:
        return

    dynamo.upsert(
        IDENTITY_TABLE,
        {"contact_id": str(contact_id)},
        UpdateExpression="SET preferred_language = :language",
        ExpressionAttributeValues={":language": language},
    )
    log.info("preferred language updated", language=language, status="OK")


def _reconcile_transfer(conversation_id: str, payload: dict) -> None:
    """
    Records a callback for a transfer that failed and took the call with it.

    conversation_id: the call.
    payload:         the post-call payload.

    Returns: nothing.

    The agent promises a callback before it transfers, precisely because a transfer can drop
    the call without warning. When it does, nothing on the call is left to keep that promise,
    and this is the last place that knows it was made (FR-020, research D4).
    """
    attempted, succeeded = performance_domain.transfer(payload)
    if not attempted or succeeded:
        return

    record = dynamo.get(CONVERSATIONS_TABLE, {"conversation_id": conversation_id}) or {}
    if record.get("callback"):
        return

    escalation = record.get("escalation") or {}
    conversation_state.record_callback(
        conversation_id,
        customer_id=payload.get("customer_id"),
        reason="TRANSFER_FAILED",
        ticket_id=escalation.get("ticket_id"),
    )
    log.info(
        "callback recorded after a failed transfer",
        conversation_id=conversation_id,
        status="RECONCILED",
    )

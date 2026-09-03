"""The verify_identity tool endpoint.

Thin by design: parse, look up, call the pure rule, record the effects, return a status.
Every financial tool downstream asks the conversations table whether this call succeeded, so
this handler is the only place identity is established and the only place the lockout
counter moves.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from src.adapters import dynamo, secrets
from src.adapters.errors import ErrorCategory, ToolError
from src.common import auth, conversation_state
from src.common import logging as log
from src.common.conversation_state import VerificationStatus as ConversationVerification
from src.domain import policy as policy_module
from src.domain.verification import (
    Factor,
    VerificationStatus,
    check_factors,
)

IDENTITY_TABLE = "customer-identity"

# Factors the caller may supply. Anything else in the payload is ignored rather than
# rejected: a language model will occasionally invent a field name, and failing the call for
# it would turn a harmless hallucination into a refused customer.
SUPPORTED_FACTORS = {factor.value: factor for factor in Factor}


def handler(event: dict, _context: Any = None) -> dict:
    """
    Evaluates a caller's identifying answers and records the outcome for the conversation.

    event: API Gateway proxy event. Body carries conversation_id and a list of
           {field, value} factors, per contracts/tools.md.

    Returns: an API Gateway response whose body is the verification status, the confirmed
             and required counts, and the next field to ask for. Never a stored value, and
             never which factor was wrong (FR-004).
    """
    try:
        body = json.loads(event.get("body") or "{}")
        auth.require_api_key(event.get("headers") or {}, secrets.get("tools/api-key"))

        conversation_id = body.get("conversation_id")
        if not conversation_id:
            raise ToolError(ErrorCategory.VALIDATION, "missing conversation_id")

        result = _verify(conversation_id, body)
        return _response(200, result)

    except ToolError as error:
        log.error("verify_identity failed", error_category=str(error.category))
        return _response(200, error.to_response())


def _verify(conversation_id: str, body: dict) -> dict:
    """
    Resolves the customer, applies the rule, and records the effects.

    conversation_id: the call in progress.
    body:            the parsed request.

    Returns: the response body defined in contracts/tools.md.
    """
    settings = policy_module.load()
    supplied = _parse_factors(body.get("factors") or [])
    customer_id = _resolve_customer(body, supplied)

    # An unknown customer is carried through the rule with an empty record rather than
    # returned early, so "no such customer" produces the same response as wrong answers and
    # the gate cannot be used to enumerate customers.
    record = dynamo.get(IDENTITY_TABLE, {"customer_id": customer_id}) if customer_id else None
    stored = _stored_factors(record) if record else {}

    if record and _is_locked(record):
        log.info("verification locked", conversation_id=conversation_id, status="LOCKED")
        return _body(VerificationStatus.LOCKED, 0, settings.required_factor_count, None, False)

    outcome = check_factors(
        supplied=supplied,
        stored=stored,
        required_count=settings.required_factor_count,
    )

    if record:
        _record_attempt(record, outcome, settings.verification_max_attempts)

    # Counted against the call as well as the customer. Without this a caller who never
    # names an account has no counter to exhaust and can guess indefinitely (FR-006).
    if outcome.is_failed_attempt:
        attempts = conversation_state.record_failed_attempt(conversation_id)
        if attempts >= settings.verification_max_attempts:
            log.info("conversation locked", conversation_id=conversation_id, status="LOCKED")
            return _body(VerificationStatus.LOCKED, 0, settings.required_factor_count, None, False)

    if outcome.status is VerificationStatus.VERIFIED and customer_id:
        conversation_state.set_verification(
            conversation_id,
            ConversationVerification.VERIFIED,
            customer_id,
            display=_display_fields(record or {}),
        )

    log.info(
        "verification evaluated",
        conversation_id=conversation_id,
        status=str(outcome.status),
        attempt=outcome.confirmed_count,
    )

    return _body(
        outcome.status,
        outcome.confirmed_count,
        outcome.required_count,
        outcome.next_factor_hint,
        outcome.non_document_satisfied,
    )


def _display_fields(record: dict) -> dict[str, str]:
    """
    The few non-sensitive fields later tools need about a verified customer.

    record: the identity record.

    Returns: company name, language, account status and the HubSpot ids. Carried onto the
             conversation so that no other handler needs permission on the identity table —
             which is what keeps 'only two functions can read identity data' true rather
             than aspirational (Principle IV).
    """
    fields = (
        "company_name",
        "preferred_language",
        "account_status",
        "hubspot_contact_id",
        "hubspot_company_id",
    )
    return {f: str(record[f]) for f in fields if record.get(f) is not None}


def _parse_factors(factors: list) -> dict[Factor, str]:
    """
    Turns the wire format into the rule's input.

    factors: list of {field, value} as sent by the agent.

    Returns: factor to spoken value. Unrecognised field names are dropped, not rejected.
    """
    parsed: dict[Factor, str] = {}
    for item in factors:
        field = SUPPORTED_FACTORS.get(str(item.get("field", "")).strip().lower())
        value = item.get("value")
        if field and isinstance(value, str) and value.strip():
            parsed[field] = value
    return parsed


def _resolve_customer(body: dict, supplied: dict[Factor, str]) -> str | None:
    """
    Works out whose record to check the answers against.

    body:     the request, which may carry the candidate customer id derived from the
              caller's phone number by the initiation webhook.
    supplied: the caller's answers.

    Returns: a customer id, or None when there is nothing to look up.

    A supplied customer id wins over the caller-id candidate. The candidate exists to pick a
    greeting language (FR-033b); letting it silently select the record would make the phone
    number a de facto factor. It may still scope the lookup when the caller offers no id,
    because doing so grants nothing: three correct factors are still required against
    whichever record is chosen.
    """
    if Factor.CUSTOMER_ID in supplied:
        return supplied[Factor.CUSTOMER_ID].strip().upper()

    candidate = body.get("candidate_customer_id")
    return candidate.strip() if isinstance(candidate, str) and candidate.strip() else None


def _stored_factors(record: dict) -> dict[Factor, str]:
    """Extracts only the comparable fields from the identity record, so nothing else can be
    reached by the rule."""
    return {
        factor: str(record[factor.value])
        for factor in Factor
        if record.get(factor.value) is not None
    }


def _is_locked(record: dict) -> bool:
    """Whether the account is inside a lockout window set by earlier failures (FR-006)."""
    locked_until = record.get("locked_until")
    if not locked_until:
        return False
    return datetime.fromisoformat(str(locked_until)) > datetime.now(UTC)


def _record_attempt(record: dict, outcome, max_attempts: int) -> None:
    """
    Moves the failure counter, and locks the account when it is exhausted.

    record:       the identity record just read.
    outcome:      the rule's decision.
    max_attempts: how many wrong attempts are tolerated, from policy.

    Returns: nothing. A successful verification resets the counter; only a wrong answer
             advances it, so a caller who simply knows fewer facts is never locked out.
    """
    customer_id = record["customer_id"]

    if outcome.status is VerificationStatus.VERIFIED:
        dynamo.update_if(
            IDENTITY_TABLE,
            {"customer_id": customer_id},
            condition="attribute_exists(customer_id)",
            UpdateExpression="SET failed_verification_attempts = :zero REMOVE locked_until",
            ExpressionAttributeValues={":zero": 0},
        )
        return

    if not outcome.is_failed_attempt:
        return

    attempts = int(record.get("failed_verification_attempts", 0)) + 1

    if attempts >= max_attempts:
        # Locking is what turns a guessing game into a bounded one. The window is
        # deliberately long enough that a caller cannot simply redial past it.
        locked_until = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
        dynamo.update_if(
            IDENTITY_TABLE,
            {"customer_id": customer_id},
            condition="attribute_exists(customer_id)",
            UpdateExpression="SET failed_verification_attempts = :n, locked_until = :until",
            ExpressionAttributeValues={":n": attempts, ":until": locked_until},
        )
        log.info("account locked", customer_id=customer_id, status="LOCKED", attempt=attempts)
        return

    dynamo.update_if(
        IDENTITY_TABLE,
        {"customer_id": customer_id},
        condition="attribute_exists(customer_id)",
        UpdateExpression="SET failed_verification_attempts = :n",
        ExpressionAttributeValues={":n": attempts},
    )


def _body(status, confirmed: int, required: int, hint, non_document: bool) -> dict:
    """Builds the response defined in contracts/tools.md."""
    return {
        "status": str(status),
        "factors_confirmed": confirmed,
        "factors_required": required,
        "next_factor_hint": str(hint) if hint else None,
        "non_document_factor_satisfied": non_document,
    }


def _response(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }

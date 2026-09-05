"""Checks one identifying detail, so the call can recover from how it was heard.

Deliberately separate from verify_identity, which still makes the decision. This endpoint
exists because a voice call loses things a text one does not: a Swiss surname arrives
misspelled, and "11 6 1994" means two different days depending on who transcribed it. Both
are recoverable if caught while the caller is still on that question, and neither is
recoverable afterwards.

It tells the caller whether one detail was found, which the final verification never does.
That is an enumeration oracle and a deliberate cost: it is what buys the spelling and date
recovery. The bar itself has not moved -- knowing an email exists confirms nothing about who
is holding the phone, and verify_identity still requires the full set.
"""

import json
from typing import Any

from src.adapters import secrets
from src.adapters.errors import ErrorCategory, ToolError
from src.common import auth, conversation_state, guessing, identity, validation
from src.common import logging as log
from src.domain import policy as policy_module
from src.domain.verification import (
    Factor,
    ambiguous_date,
    check_factors,
)

CHECKABLE = {Factor.EMAIL, Factor.PHONE, Factor.DATE_OF_BIRTH}


def handler(event: dict, _context: Any = None) -> dict:
    """
    Checks a single detail and says whether it landed.

    event: API Gateway proxy event carrying conversation_id, field and value.

    Returns: an API Gateway response carrying MATCHED, NOT_MATCHED or AMBIGUOUS.
    """
    try:
        body = json.loads(event.get("body") or "{}")
        auth.require_api_key(event.get("headers") or {}, secrets.get("tools/api-key"))

        conversation_id = str(validation.require(body, "conversation_id"))
        field = str(validation.require(body, "field"))
        value = str(validation.require(body, "value")).strip()

        settings = policy_module.load()
        if field not in {f.value for f in CHECKABLE}:
            raise ToolError(ErrorCategory.VALIDATION, f"not a checkable field: {field}")

        return _response(200, _check(conversation_id, Factor(field), value, settings))

    except ToolError as error:
        log.error(
            "check_factor failed",
            error_category=str(error.category),
            error_detail=error.detail,
        )
        return _response(200, error.to_response())


def _check(conversation_id: str, factor: Factor, value: str, settings) -> dict:
    """
    Decides what to tell the agent about one answer.

    conversation_id: the call.
    factor:          which detail.
    value:           what the caller said.
    settings:        policy, for the guessing allowance.

    Returns: the response body.
    """
    # Counted here as well as at the final check. A caller offered three different dates of
    # birth through this endpoint and tripped nothing, because only verify_identity was
    # counting and they never reached it. Checking each detail separately is what made that
    # possible, so it is what has to count.
    enumerating, _ = guessing.record_and_check(
        conversation_id,
        {factor: value},
        settings,
        conversation_state.resolved_contact(conversation_id),
    )
    if enumerating:
        log.info("conversation locked", conversation_id=conversation_id, status="LOCKED")
        return {"status": "LOCKED", "field": factor.value}

    # Asked before anything is looked up, because it is a question about the sentence rather
    # than about the caller. A date nobody can read two ways is not clarified.
    if factor is Factor.DATE_OF_BIRTH:
        readings = ambiguous_date(value)
        if readings:
            log.info("date is ambiguous", conversation_id=conversation_id)
            return {
                "status": "AMBIGUOUS",
                "readings": list(readings),
                "field": factor.value,
            }

    # Whoever the first identifier resolved to is who the rest of this call is checked
    # against. Re-resolving on every detail let a caller give one person's email and another
    # person's phone and be told both matched, because each was compared against a different
    # record. The final check would still have refused them, but MATCHED said otherwise.
    contact_id = conversation_state.resolved_contact(conversation_id)
    if not contact_id and factor in identity.INDEXES:
        contact_id = identity.lookup_contact(factor, value)
        if contact_id:
            conversation_state.set_resolved_contact(conversation_id, contact_id)

    matched = _compares(contact_id, factor, value)
    log.info(
        "factor checked",
        conversation_id=conversation_id,
        field=factor.value,
        status="MATCHED" if matched else "NOT_MATCHED",
    )
    return {"status": "MATCHED" if matched else "NOT_MATCHED", "field": factor.value}


def _compares(contact_id: str | None, factor: Factor, value: str) -> bool:
    """
    Compares one answer against the resolved record.

    contact_id: whose record, or None when nothing has resolved yet.
    factor:     which detail.
    value:      what the caller said.

    Returns: whether it matches. False when no record has resolved, which is the same answer
             a wrong value gets: this endpoint says whether a detail landed, never why it
             did not.
    """
    if not contact_id:
        return False

    record = identity.load_record(contact_id)
    if not record or record.get(factor.value) is None:
        return False

    outcome = check_factors(
        supplied={factor: value},
        stored={factor: str(record[factor.value])},
        required_count=1,
    )
    return not outcome.mismatched_factors


def _response(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }

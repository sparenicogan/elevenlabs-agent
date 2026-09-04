"""The verify_identity tool endpoint.

Thin by design: parse, look up, call the pure rule, record the effects, return a status.
Every financial tool downstream asks the conversations table whether this call succeeded, so
this handler is the only place identity is established and the only place the lockout
counter moves.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from boto3.dynamodb.conditions import Key

from src.adapters import dynamo, secrets
from src.adapters.errors import ErrorCategory, ToolError
from src.common import auth, conversation_state
from src.common import logging as log
from src.common.conversation_state import VerificationStatus as ConversationVerification
from src.domain import policy as policy_module
from src.domain.risk import RiskSignal, SignalType
from src.domain.verification import (
    Factor,
    VerificationStatus,
    check_factors,
    fingerprint,
    is_enumerating,
    lookup_key,
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
        log.error(
            "verify_identity failed",
            error_category=str(error.category),
            error_detail=error.detail,
        )
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
    contact_id = _resolve_contact(body, supplied)

    # Checked before the answers are evaluated. A caller working through values must not be
    # able to learn which of them was right on the attempt that stops them.
    enumerated, offered_new = _check_for_enumeration(
        conversation_id, contact_id, supplied, settings
    )
    if enumerated:
        return _body(VerificationStatus.LOCKED, 0, settings.required_factor_count, None, False)

    # An unknown customer is carried through the rule with an empty record rather than
    # returned early, so "no such customer" produces the same response as wrong answers and
    # the gate cannot be used to enumerate customers.
    record = dynamo.get(IDENTITY_TABLE, {"contact_id": contact_id}) if contact_id else None
    stored = _stored_factors(record) if record else {}

    if record and _is_locked(record):
        log.info("verification locked", conversation_id=conversation_id, status="LOCKED")
        return _body(VerificationStatus.LOCKED, 0, settings.required_factor_count, None, False)

    outcome = check_factors(
        supplied=supplied,
        stored=stored,
        required_count=settings.required_factor_count,
    )

    # Distinct wrong values per field, not failed calls and not a total across fields. Three
    # answers offered together that cannot be matched is one failed attempt, not three.
    wrong_count = conversation_state.record_wrong_values(
        conversation_id, _wrong_fingerprints(supplied, outcome)
    )

    # Called on every attempt, not only failing ones: a successful verification is what
    # clears the counter, and guarding this on a failure would leave a caller who eventually
    # got in still carrying their earlier mistakes into the next call.
    if record:
        _record_attempt(record, outcome, settings.verification_max_attempts, wrong_count)

    # Counted against the call as well as the customer. Without this a caller who never
    # names an account has no counter to exhaust and can guess indefinitely (FR-006).
    if wrong_count >= settings.verification_max_attempts:
        log.info(
            "conversation locked",
            conversation_id=conversation_id,
            status="LOCKED",
            attempt=wrong_count,
        )
        return _body(VerificationStatus.LOCKED, 0, settings.required_factor_count, None, False)

    candidate = body.get("candidate_customer_id")
    if candidate and contact_id and candidate.strip() and candidate.strip() != contact_id:
        # The number they called from belongs to one account and they named another. Innocent
        # explanations exist — a shared switchboard, a colleague's desk — so it is recorded
        # rather than acted on (research D3).
        conversation_state.record_risk_signal(
            RiskSignal(
                signal_type=SignalType.CONFLICTING_IDENTITY_DATA,
                evidence="caller number resolves to a different account than the one named",
                conversation_id=conversation_id,
                customer_id=contact_id,
            )
        )

    if outcome.status is VerificationStatus.VERIFIED and record:
        # The account is the contact's company. A person authenticates; a company account is
        # what they reach.
        conversation_state.set_verification(
            conversation_id,
            ConversationVerification.VERIFIED,
            str(record["account_id"]),
            display=_display_fields(record),
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
        outcome.personal_satisfied,
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
        # Which person verified. It reaches the audit record and the handoff, so a human
        # knows who they were speaking to rather than only which account.
        "first_name",
        "last_name",
        "contact_id",
    )
    return {f: str(record[f]) for f in fields if record.get(f) is not None}


def _wrong_fingerprints(supplied: dict[Factor, str], outcome) -> dict[str, str]:
    """
    Fingerprints only the answers that did not match.

    supplied: this attempt's answers.
    outcome:  the rule's decision, which knows internally which factors mismatched.

    Returns: field name to fingerprint, for the wrong values only, so the lockout counts how
             many different things a caller has tried for one field rather than how many
             times they were told no.

    The mismatched set is used here and nowhere else. It never reaches a response: telling a
    caller which answer failed is precisely the oracle the gate exists to deny them (FR-004).
    """
    if not outcome.mismatched_factors:
        return {}

    salt = secrets.get("verification/attempt-salt")
    return {
        factor.value: fingerprint(factor, supplied[factor], salt)
        for factor in outcome.mismatched_factors
        if factor in supplied
    }


def _check_for_enumeration(
    conversation_id: str,
    contact_id: str | None,
    supplied: dict[Factor, str],
    settings,
) -> tuple[bool, bool]:
    """
    Records what has been offered for each field and decides whether the caller is guessing.

    conversation_id: the call.
    contact_id:      the person, where one was resolved. A caller who matches nobody still
                     produces signals; they belong to the conversation.
    supplied:        this attempt's answers.
    settings:        policy, for the allowance.

    Returns: (whether a field exceeded the allowance, whether this call offered any value not
             already seen). The second is what stops a resent wrong answer being counted as a
             fresh failure.

    One correction is human. A third distinct value for the same field is someone working
    through possibilities, and the difference matters more than any single wrong answer does
    (FR-006a).
    """
    if not supplied:
        return False, False

    salt = secrets.get("verification/attempt-salt")
    counts, offered_new = conversation_state.record_factor_attempts(
        conversation_id,
        {factor.value: fingerprint(factor, value, salt) for factor, value in supplied.items()},
    )

    offending = is_enumerating(
        {Factor(field): count for field, count in counts.items()},
        settings.guessing_max_distinct_values,
    )
    if not offending:
        return False, offered_new

    conversation_state.record_risk_signal(
        RiskSignal(
            signal_type=SignalType.SUSPECTED_GUESSING,
            # A count and a field name. Never what was offered — recording the guesses would
            # defeat the point of fingerprinting them.
            evidence=(
                f"{counts[offending.value]} distinct values offered for {offending.value}, "
                f"allowance is {settings.guessing_max_distinct_values}"
            ),
            conversation_id=conversation_id,
            customer_id=contact_id,
        )
    )
    log.info(
        "suspected guessing",
        conversation_id=conversation_id,
        customer_id=contact_id or "",
        status="LOCKED",
        attempt=counts[offending.value],
    )
    return True, offered_new


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


def _resolve_contact(body: dict, supplied: dict[Factor, str]) -> str | None:
    """
    Works out which person's record to check the answers against.

    body:     the request, which may carry the candidate customer id derived from the
              caller's phone number by the initiation webhook.
    supplied: the caller's answers.

    Returns: a contact id, or None when nothing supplied identifies a person.

    Resolution tries every identifier the caller might reasonably have given, not only the
    customer id. Requiring the id first meant a caller who led with their email — which most
    people know and few can look up — had that correct answer scored as wrong, because there
    was no record to compare it against yet.

    A supplied identifier always wins over the caller-id candidate. The candidate exists to
    pick a greeting language (FR-033b); letting it select the record would make the phone
    number a de facto factor. It may still scope the lookup when the caller offers nothing
    else, because doing so grants nothing: three correct factors are still required against
    whichever record is chosen.
    """
    for factor, index, attribute in (
        (Factor.EMAIL, "email-index", "email_lookup"),
        (Factor.PHONE, "phone-index", "phone_lookup"),
    ):
        if factor in supplied:
            resolved = _lookup(factor, index, attribute, supplied[factor])
            if resolved:
                return resolved

    candidate = body.get("candidate_customer_id")
    return candidate.strip() if isinstance(candidate, str) and candidate.strip() else None


def _lookup(factor: Factor, index: str, attribute: str, value: str) -> str | None:
    """
    Finds a customer by one of the identifiers they might quote.

    factor:    which identifier, so it is normalised the way the comparison normalises it.
    index:     the secondary index to query.
    attribute: its hash key, which holds the normalised form.
    value:     what the caller said.

    Returns: the contact id, or None when nothing matches — which is deliberately
             indistinguishable downstream from an answer that matched nothing, since a
             response that distinguished them would say whether an address is on file.

    More than one hit resolves nobody. Normalisation merges addresses that differ only by a
    hyphen, and picking one of two people arbitrarily would let a caller be checked against a
    record that is not theirs.
    """
    hits = dynamo.query(
        IDENTITY_TABLE,
        index=index,
        KeyConditionExpression=Key(attribute).eq(lookup_key(factor, value)),
    )
    return hits[0]["contact_id"] if len(hits) == 1 else None


def _stored_factors(record: dict) -> dict[Factor, str]:
    """Extracts only the comparable fields from the identity record, so nothing else can be
    reached by the rule."""
    stored = {
        factor: str(record[factor.value])
        for factor in Factor
        if record.get(factor.value) is not None
    }

    # The customer id names the company, not the person, so it is compared against the
    # account this contact belongs to. A caller giving only their customer id has said which
    # company they are calling about and nothing about who they are — which is why it cannot
    # resolve a contact on its own.
    if record.get("account_id"):
        stored[Factor.CUSTOMER_ID] = str(record["account_id"])

    return stored


def _is_locked(record: dict) -> bool:
    """Whether the account is inside a lockout window set by earlier failures (FR-006)."""
    locked_until = record.get("locked_until")
    if not locked_until:
        return False
    return datetime.fromisoformat(str(locked_until)) > datetime.now(UTC)


def _record_attempt(record: dict, outcome, max_attempts: int, wrong_count: int) -> None:
    """
    Moves the failure counter, and locks the account when it is exhausted.

    record:       the identity record just read.
    outcome:      the rule's decision.
    max_attempts: how many wrong attempts are tolerated, from policy.
    wrong_count:  how many distinct wrong values this call has produced.

    Returns: nothing. A successful verification resets the counter; only distinct wrong
             values advance it, so a caller who knows fewer facts, or who has one wrong
             answer resent on every turn, is never locked out for it.
    """
    contact_id = record["contact_id"]

    if outcome.status is VerificationStatus.VERIFIED:
        dynamo.update_if(
            IDENTITY_TABLE,
            {"contact_id": contact_id},
            condition="attribute_exists(contact_id)",
            UpdateExpression="SET failed_verification_attempts = :zero REMOVE locked_until",
            ExpressionAttributeValues={":zero": 0},
        )
        return

    if not outcome.is_failed_attempt:
        return

    attempts = wrong_count

    if attempts >= max_attempts:
        # Locking is what turns a guessing game into a bounded one. The window is
        # deliberately long enough that a caller cannot simply redial past it.
        locked_until = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
        dynamo.update_if(
            IDENTITY_TABLE,
            {"contact_id": contact_id},
            condition="attribute_exists(contact_id)",
            UpdateExpression="SET failed_verification_attempts = :n, locked_until = :until",
            ExpressionAttributeValues={":n": attempts, ":until": locked_until},
        )
        log.info("account locked", customer_id=contact_id, status="LOCKED", attempt=attempts)
        return

    dynamo.update_if(
        IDENTITY_TABLE,
        {"contact_id": contact_id},
        condition="attribute_exists(contact_id)",
        UpdateExpression="SET failed_verification_attempts = :n",
        ExpressionAttributeValues={":n": attempts},
    )


def _body(status, confirmed: int, required: int, hint, personal: bool) -> dict:
    """Builds the response defined in contracts/tools.md."""
    return {
        "status": str(status),
        "factors_confirmed": confirmed,
        "factors_required": required,
        "next_factor_hint": str(hint) if hint else None,
        "personal_factor_satisfied": personal,
    }


def _response(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }

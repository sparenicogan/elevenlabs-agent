"""The conversation initiation webhook.

Called when a call arrives, before anyone has spoken. It answers one question: which language
to greet in. It also passes the customer the number is registered to, as a secret variable,
so the backend can note later whether the caller turned out to be someone else.

That candidate carries no weight. `verify_identity` re-derives the customer from what the
caller actually says, and a mismatch is recorded as a risk signal rather than treated as an
error — a shared switchboard and a borrowed phone are both ordinary (FR-033b, research D3).
"""

import json
from typing import Any

from boto3.dynamodb.conditions import Key

from src.adapters import dynamo
from src.adapters.errors import ToolError
from src.common import logging as log
from src.domain.locale import normalise
from src.domain.verification import Factor, lookup_key

IDENTITY_TABLE = "customer-identity"


def handler(event: dict, _context: Any = None) -> dict:
    """
    Chooses the greeting language for an arriving call.

    event: API Gateway proxy event carrying caller_id and agent_id.

    Returns: dynamic variables and a language override, per contracts/webhooks.md.

    Never fails the call. An unrecognised number, an unreachable table and a malformed request
    all produce the same answer as a recognised number would: a greeting in the fallback
    language. A caller hearing German when they expected French is a small annoyance; a caller
    hearing nothing is a lost call.
    """
    try:
        body = json.loads(event.get("body") or "{}")
        caller_id = str(body.get("caller_id") or "")
        contact = _contact_for(caller_id) if caller_id else None
    except (ToolError, ValueError) as error:
        log.error("initiation lookup failed", error_detail=str(error), status="DEGRADED")
        contact = None

    language = normalise((contact or {}).get("preferred_language"))
    log.info(
        "call initiated",
        language=language,
        status="RECOGNISED" if contact else "UNRECOGNISED",
    )

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "dynamic_variables": {
                    # secret__ so it stays out of the transcript and the model's context. The
                    # agent must never greet by name or let on that the number was known.
                    "secret__candidate_customer_id": (contact or {}).get("account_id"),
                    "greeting_language": language,
                },
                "conversation_config_override": {"agent": {"language": language}},
            }
        ),
    }


def _contact_for(caller_id: str) -> dict | None:
    """
    Finds the contact a number is registered to.

    caller_id: the calling number, in whatever form the carrier sent it.

    Returns: the identity record, or None when the number matches nobody or matches more than
             one person. Normalised the same way every other lookup is, so a number reaching
             this webhook and the same number spoken aloud resolve identically.
    """
    hits = dynamo.query(
        IDENTITY_TABLE,
        index="phone-index",
        KeyConditionExpression=Key("phone_lookup").eq(lookup_key(Factor.PHONE, caller_id)),
    )
    if len(hits) != 1:
        return None

    return dynamo.get(IDENTITY_TABLE, {"contact_id": hits[0]["contact_id"]})

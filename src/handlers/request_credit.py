"""The request_credit tool endpoint.

The one place the agent gives something away without a person confirming it. Everything
about its shape follows from that.

Evaluation and issuance are a single operation deliberately. Two tools — one to check
eligibility, one to issue — would leave a window in which the model calls the second without
the first, and the agent is exactly the component that must not be trusted to sequence a
financial decision correctly (research D1). There is no endpoint here that issues a credit
without deciding whether it may.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key

from src.adapters import dynamo, hubspot, secrets
from src.adapters.errors import ErrorCategory, ToolError
from src.common import audit, auth, conversation_state, idempotency, validation
from src.common import logging as log
from src.domain import policy as policy_module
from src.domain.credit import (
    CreditOutcome,
    entry_from_ledger,
    evaluate_credit,
    rolling_credit_total,
)
from src.domain.risk import evaluate as evaluate_risk

LEDGER_TABLE = "ledger"
AGENT_VERSION = "1.0.0"

# Outcomes that should reach a person rather than simply ending the conversation. A refused
# customer who is told only "no" has been given nothing; the point of escalating is that
# somebody can still say yes.
ESCALATING_OUTCOMES = frozenset(
    {CreditOutcome.DENIED_LIMIT, CreditOutcome.DENIED_RISK, CreditOutcome.DENIED_EXCEEDS_ENTRY}
)


def handler(event: dict, _context: Any = None) -> dict:
    """
    Evaluates a goodwill credit request and issues it if policy allows.

    event: API Gateway proxy event carrying conversation_id, entry_id, amount and reason.

    Returns: an API Gateway response carrying GRANTED or a denial reason, the rule that
             decided it, and the customer's rolling total.
    """
    try:
        body = json.loads(event.get("body") or "{}")
        auth.require_api_key(event.get("headers") or {}, secrets.get("tools/api-key"))

        conversation_id = body.get("conversation_id")
        if not conversation_id:
            raise ToolError(ErrorCategory.VALIDATION, "missing conversation_id")

        customer_id, display = conversation_state.verified_context(conversation_id)
        return _response(200, _request(conversation_id, customer_id, display, body))

    except ToolError as error:
        log.error(
            "request_credit failed",
            error_category=str(error.category),
            error_detail=error.detail,
        )
        return _response(200, error.to_response())


def _request(conversation_id: str, customer_id: str, display: dict, body: dict) -> dict:
    """
    Assembles the facts, applies the rule, and writes the credit if it passes.

    conversation_id: the call, and the idempotency anchor.
    customer_id:     from verification, never from the request.
    display:         company name and CRM ids, recorded at verification.
    body:            the parsed request.

    Returns: the response body from contracts/tools.md.
    """
    settings = policy_module.load()
    entry_id = str(validation.require(body, "entry_id"))
    amount = validation.money(body, "amount")
    reason = str(body.get("reason") or "").strip()

    ledger = _customer_ledger(customer_id)
    credits = [e for e in ledger if e.get("type") == "CREDIT_NOTE"]

    decision = evaluate_credit(
        requested_amount=amount,
        entry=entry_from_ledger(_find(ledger, entry_id), credits),
        account_status=str(display.get("account_status", "UNKNOWN")),
        rolling_total=rolling_credit_total(credits, date.today(), settings.credit_window_months),
        risk_level=evaluate_risk(conversation_state.risk_signals(conversation_id)),
        max_per_request=settings.credit_max_per_request,
        max_rolling=settings.credit_max_rolling,
    )

    if decision.outcome is CreditOutcome.GRANTED:
        _issue(conversation_id, customer_id, entry_id, decision, reason, display)

    log.info(
        "credit evaluated",
        conversation_id=conversation_id,
        customer_id=customer_id,
        entry_id=entry_id,
        status=str(decision.outcome),
        rule_applied=decision.authorizing_rule,
    )

    return {
        "status": str(decision.outcome),
        "credit_entry_id": _credit_id(conversation_id, entry_id, amount)
        if decision.outcome is CreditOutcome.GRANTED
        else None,
        "rule_applied": decision.authorizing_rule,
        "rolling_total_after": float(decision.rolling_total_after),
        # Tells the agent to hand over rather than simply refuse. A customer told only "no"
        # has been given nothing, and somebody with more authority may still say yes.
        "should_escalate": decision.outcome in ESCALATING_OUTCOMES,
    }


def _issue(
    conversation_id: str,
    customer_id: str,
    entry_id: str,
    decision,
    reason: str,
    display: dict,
) -> None:
    """
    Writes the credit note, audits it, and logs the interaction.

    conversation_id: the call.
    customer_id:     the verified customer.
    entry_id:        the charge the credit attaches to.
    decision:        the evaluated CreditDecision.
    reason:          the caller's stated reason, in their words.
    display:         CRM identifiers.

    Returns: nothing.

    The credit note carries a deterministic id derived from the conversation, the charge and
    the amount, and is written only if absent. A caller who repeats themselves, or a dropped
    call redialled, cannot be credited twice for the same request (FR-022).
    """
    credit_entry_id = _credit_id(conversation_id, entry_id, decision.credit_amount)

    written = dynamo.put_if_absent(
        LEDGER_TABLE,
        {
            "customer_id": customer_id,
            "entry_id": credit_entry_id,
            "type": "CREDIT_NOTE",
            # Negative, because a credit reduces what is owed.
            "amount": -decision.credit_amount,
            "currency": "CHF",
            "entry_date": datetime.now(UTC).date().isoformat(),
            "status": "APPROVED",
            "allocated_to": [entry_id],
            "reason": reason or "goodwill",
            "originating_conversation_id": conversation_id,
            "decision_source": "AGENT_AUTONOMOUS",
            "approval_status": "NOT_REQUIRED",
        },
        key_field="entry_id",
    )

    if not written:
        # The same request, already granted. Nothing further to do, and nothing to correct.
        return

    audit.write(
        audit.AuditEvent(
            action="issue_credit",
            previous_state="NONE",
            new_state="APPROVED",
            authorizing_rule=decision.authorizing_rule,
            customer_id=customer_id,
            conversation_id=conversation_id,
            agent_version=AGENT_VERSION,
            risk_result="NOT_HIGH",
            # The only autonomous financial action in the system, and the audit record says
            # so explicitly rather than by omission.
            human_approval_required=False,
            entry_id=credit_entry_id,
        )
    )

    contact_id = display.get("hubspot_contact_id")
    if contact_id:
        try:
            hubspot.log_interaction(
                contact_id,
                f"Goodwill credit of CHF {decision.credit_amount:,.2f} applied to {entry_id} "
                f"during a call. Reason given: {reason or 'not stated'}.",
            )
        except ToolError as error:
            # The credit exists and is audited. A CRM outage must not undo it.
            log.error(
                "interaction not logged",
                error_category=str(error.category),
                status="DEGRADED",
            )


def _credit_id(conversation_id: str, entry_id: str, amount: Decimal) -> str:
    """
    Builds a deterministic identifier for this credit.

    Derived from the conversation, the charge and the amount, so the same request always
    produces the same id and a duplicate write fails its condition rather than creating a
    second credit.
    """
    digest = idempotency.key(conversation_id, entry_id, str(amount))
    return f"cn_{digest[:20]}"


def _customer_ledger(customer_id: str) -> list[dict]:
    """
    Reads every ledger entry for the customer.

    The whole ledger rather than a filtered slice, because the decision needs the charge, the
    credits already on it, and the rolling window total — three different views of the same
    rows, and one read is cheaper than three queries.
    """
    return dynamo.query(LEDGER_TABLE, KeyConditionExpression=Key("customer_id").eq(customer_id))


def _find(entries: list[dict], entry_id: str) -> dict | None:
    """Finds one entry by id, or None. Ownership is implicit: the query was scoped to the
    verified customer, so an entry belonging to anyone else is simply not here (FR-007)."""
    return next((e for e in entries if e.get("entry_id") == entry_id), None)


def _response(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }

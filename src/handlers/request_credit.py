"""The request_credit tool endpoint.

The agent decides whether a credit is permitted; a person decides whether it is given. The
rules run here in full, and their answer is recorded as a ticket rather than a ledger entry,
so the worst this endpoint can do is ask.

Evaluation and issuance are a single operation deliberately. Two tools — one to check
eligibility, one to issue — would leave a window in which the model calls the second without
the first, and the agent is exactly the component that must not be trusted to sequence a
financial decision correctly (research D1). There is no endpoint here that issues a credit
without deciding whether it may.
"""

import json
from datetime import date
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key

from src.adapters import dynamo, hubspot, secrets
from src.adapters.errors import ErrorCategory, ToolError
from src.common import audit, auth, call_history, conversation_state, http, validation
from src.common import logging as log
from src.domain import policy as policy_module
from src.domain.credit import (
    CreditOutcome,
    entry_from_ledger,
    evaluate_credit,
    rolling_credit_total,
)
from src.domain.risk import detect_history_signals
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
        return http.respond(200, _request(conversation_id, customer_id, display, body))

    except ToolError as error:
        return http.failed("request_credit", error)


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
    pending = _pending_requests(display)
    # Applied credits and unanswered requests, together. A colleague who rang this morning
    # has already spoken for part of the ceiling even though nothing has reached the ledger.
    credits = [e for e in ledger if e.get("type") == "CREDIT_NOTE"] + _as_credit_notes(pending)

    # Detected before the decision, so a pattern in the history can override rules that
    # would otherwise permit the request. Signals raised earlier in this call — a lockout, a
    # caller working through values — are already on the conversation and count too.
    history_signals = detect_history_signals(
        ledger=ledger,
        conversations=call_history.recent(customer_id),
        customer_id=customer_id,
        conversation_id=conversation_id,
        today=date.today(),
        max_per_request=settings.credit_max_per_request,
    )
    for signal in history_signals:
        conversation_state.record_risk_signal(signal)

    decision = evaluate_credit(
        requested_amount=amount,
        entry=entry_from_ledger(_find(ledger, entry_id), credits),
        account_status=str(display.get("account_status", "UNKNOWN")),
        rolling_total=rolling_credit_total(credits, date.today(), settings.credit_window_months),
        risk_level=evaluate_risk(
            conversation_state.risk_signals(conversation_id) + history_signals
        ),
        max_per_request=settings.credit_max_per_request,
        max_rolling=settings.credit_max_rolling,
    )

    ticket_id = None
    if decision.outcome is CreditOutcome.GRANTED:
        # A tool call repeated inside one turn, or a caller asking twice, must not become two
        # tickets. The open request itself is the idempotency record: same charge, same
        # amount, still undecided, so there is nothing to add by asking again.
        already = _matching_request(pending, entry_id, decision.credit_amount)
        ticket_id = already or _raise_request(
            conversation_id, customer_id, entry_id, decision, reason, display
        )

    log.info(
        "credit evaluated",
        conversation_id=conversation_id,
        customer_id=customer_id,
        entry_id=entry_id,
        status=str(decision.outcome),
        rule_applied=decision.authorizing_rule,
    )

    return {
        # REQUESTED, never GRANTED: the rules permit it, and nothing has been given yet.
        # The agent can only report what happened, and what happened is that it asked.
        "status": "REQUESTED"
        if decision.outcome is CreditOutcome.GRANTED
        else str(decision.outcome),
        "ticket_id": ticket_id,
        "rule_applied": decision.authorizing_rule,
        "rolling_total_after": float(decision.rolling_total_after),
        # Tells the agent to hand over rather than simply refuse. A customer told only "no"
        # has been given nothing, and somebody with more authority may still say yes.
        "should_escalate": decision.outcome in ESCALATING_OUTCOMES,
    }


def _pending_requests(display: dict) -> list[dict]:
    """
    Reads the credit requests this company is already waiting on.

    display: CRM identifiers recorded at verification.

    Returns: the undecided tickets, as the CRM returned them.

    Raises rather than returning an empty list when the company is unknown or the CRM cannot
    be read. An unreadable ceiling has to refuse: treating "could not tell" as "nothing
    outstanding" is how the same headroom gets spent twice.
    """
    company_id = display.get("hubspot_company_id")
    if not company_id:
        raise ToolError(ErrorCategory.INTERNAL, "no company id, cannot total pending credits")

    return hubspot.get_pending_requests(str(company_id))


def _matching_request(pending: list[dict], entry_id: str, amount: Decimal) -> str | None:
    """
    Finds an undecided request for the same credit.

    pending:  the company's open tickets.
    entry_id: the charge the credit would attach to.
    amount:   the credit the rules permitted.

    Returns: that ticket's id, or None when this request is new.
    """
    for ticket in pending:
        same_charge = str(ticket.get("related_entry_id") or "") == entry_id
        raw = ticket.get("credit_amount")
        if same_charge and raw not in (None, "") and Decimal(str(raw)) == amount:
            return str(ticket["id"])
    return None


def _as_credit_notes(pending: list[dict]) -> list[dict]:
    """
    Shapes undecided requests like ledger credit notes.

    pending: the company's open tickets.

    Returns: one entry per ticket carrying an amount, so the ceiling rules count it without
             knowing it came from the CRM rather than the ledger.
    """
    return [
        {
            "type": "CREDIT_NOTE",
            "amount": -Decimal(str(ticket["credit_amount"])),
            # PENDING_APPROVAL already counts towards both ceilings, which is exactly what an
            # unanswered request is: headroom spoken for, but not yet confirmed.
            "status": "PENDING_APPROVAL",
            # Dated today rather than from the ticket, so a request left unanswered for
            # months still counts. Erring towards refusing is the safe direction here.
            "entry_date": date.today().isoformat(),
            "allocated_to": [str(ticket.get("related_entry_id") or "")],
        }
        for ticket in pending
        if ticket.get("credit_amount") not in (None, "")
    ]


def _raise_request(
    conversation_id: str,
    customer_id: str,
    entry_id: str,
    decision,
    reason: str,
    display: dict,
) -> str | None:
    """
    Records the permitted credit as a ticket for a person to accept or reject.

    conversation_id: the call.
    customer_id:     the verified customer.
    entry_id:        the charge the credit would attach to.
    decision:        the evaluated CreditDecision, already GRANTED by the rules.
    reason:          the caller's stated reason, in their words.
    display:         CRM identifiers.

    Returns: the ticket id, or None when there is no contact to associate it with.

    credit_amount and related_entry_id are set as properties rather than described in the
    body, because the next call's ceiling is computed by summing them. A number that exists
    only in prose cannot be added up.
    """
    contact_id = display.get("hubspot_contact_id")
    if not contact_id:
        raise ToolError(ErrorCategory.INTERNAL, "no contact id, cannot record credit request")

    ticket_id = hubspot.create_ticket(
        {
            "subject": f"Goodwill credit CHF {decision.credit_amount:,.2f} on {entry_id}",
            "content": (
                f"Requested by the voice agent during conversation {conversation_id}.\n\n"
                f"Charge: {entry_id}.\n"
                f"Reason given by the caller: {reason or 'not stated'}.\n"
                f"Permitted by: {decision.authorizing_rule}.\n"
                f"Customer total after this credit, if accepted: "
                f"CHF {decision.rolling_total_after:,.2f}.\n\n"
                "The agent has told the caller this was requested, not applied. "
                "Set Request outcome to Accepted or Rejected to decide it."
            ),
            "credit_amount": float(decision.credit_amount),
            "related_entry_id": entry_id,
            "aws_customer_id": customer_id,
            "hs_pipeline_stage": "1",
            "hs_ticket_priority": "HIGH",
        },
        contact_id=str(contact_id),
        company_id=display.get("hubspot_company_id"),
    )

    audit.write(
        audit.AuditEvent(
            action="request_credit",
            previous_state="NONE",
            new_state="REQUESTED",
            authorizing_rule=decision.authorizing_rule,
            customer_id=customer_id,
            conversation_id=conversation_id,
            agent_version=AGENT_VERSION,
            risk_result="NOT_HIGH",
            # The rules permitted it; a person still has to accept it. Nothing about this
            # call moves money on its own.
            human_approval_required=True,
            entry_id=entry_id,
        )
    )

    return ticket_id


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

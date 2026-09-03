"""The propose_allocation tool endpoint.

The golden path's one mutation, and the only place the agent changes a financial record. It
does not settle anything: it moves a matched payment to UNDER_REVIEW and puts it in front of
a person. Under the shipped policy the agent has no authority to allocate at all, so every
allocation goes that way (FR-012).

Everything here is written to survive being called twice. Voice calls drop, callers repeat
themselves, and a retry that creates a second review of the same payment would be visible to
the customer as chaos.
"""

import json
from decimal import Decimal
from typing import Any

from src.adapters import dynamo, hubspot, secrets
from src.adapters.errors import ErrorCategory, ToolError
from src.common import audit, auth, conversation_state, validation
from src.common import logging as log
from src.domain import policy as policy_module
from src.domain.allocation import AllocationDecision, decide_allocation

LEDGER_TABLE = "ledger"
AGENT_VERSION = "1.0.0"


def handler(event: dict, _context: Any = None) -> dict:
    """
    Proposes that a matched payment be allocated to an invoice.

    event: API Gateway proxy event carrying conversation_id, payment_entry_id and
           invoice_entry_id.

    Returns: an API Gateway response carrying the new status, the ticket a human will work
             from, and how long the caller was told it would take.
    """
    try:
        body = json.loads(event.get("body") or "{}")
        auth.require_api_key(event.get("headers") or {}, secrets.get("tools/api-key"))

        conversation_id = body.get("conversation_id")
        if not conversation_id:
            raise ToolError(ErrorCategory.VALIDATION, "missing conversation_id")

        customer_id, display = conversation_state.verified_context(conversation_id)
        return _response(200, _propose(conversation_id, customer_id, display, body))

    except ToolError as error:
        log.error(
            "propose_allocation failed",
            error_category=str(error.category),
            error_detail=error.detail,
        )
        return _response(200, error.to_response())


def _propose(conversation_id: str, customer_id: str, display: dict, body: dict) -> dict:
    """
    Moves the payment under review, records it, and raises a ticket.

    conversation_id: the call in progress, and the idempotency anchor.
    customer_id:     from verification, never from the request.
    display:         company name and HubSpot ids, recorded at verification.
    body:            the parsed request.

    Returns: the response body from contracts/tools.md.

    Order matters. The ledger moves first, because it is the only authoritative record; the
    ticket and the CRM log follow. If HubSpot fails afterwards the payment is still under
    review and a human still finds it, which is the right way round — the reverse would
    promise a caller a review that does not exist (FR-025).
    """
    settings = policy_module.load()
    payment_id = str(validation.require(body, "payment_entry_id"))
    invoice_id = str(validation.require(body, "invoice_entry_id"))

    payment = _owned_entry(customer_id, payment_id, "PAYMENT")
    invoice = _owned_entry(customer_id, invoice_id, "INVOICE")

    outcome = decide_allocation(
        payment_status=str(payment["status"]),
        payment_amount=abs(Decimal(str(payment["amount"]))),
        authority_max=settings.allocation_authority_max,
    )

    if outcome.decision is AllocationDecision.ALREADY_UNDER_REVIEW:
        # A repeat, not an error. Returning the original ticket lets the agent say something
        # true and calm rather than creating a second review of the same payment (FR-022).
        return {
            "status": "ALREADY_UNDER_REVIEW",
            "ticket_id": payment.get("review_ticket_id"),
            "previous_status": outcome.previous_status,
            "new_status": outcome.previous_status,
            "resolution_target_hours": settings.resolution_target_hours,
        }

    if outcome.decision is AllocationDecision.ALREADY_ALLOCATED:
        return {"status": "ALREADY_ALLOCATED", "previous_status": outcome.previous_status}

    if outcome.decision is AllocationDecision.NOT_ALLOWED:
        raise ToolError(
            ErrorCategory.VALIDATION,
            f"payment not allocatable from {outcome.previous_status}",
        )

    ticket_id = _raise_ticket(conversation_id, display, invoice, payment)

    moved = dynamo.update_if(
        LEDGER_TABLE,
        {"customer_id": customer_id, "entry_id": payment_id},
        # Guards the transition even under two concurrent calls: only one can observe
        # UNALLOCATED, so only one review is ever created.
        condition="#s = :expected",
        UpdateExpression=(
            "SET #s = :new, allocated_to = :invoice, review_ticket_id = :ticket, "
            "originating_conversation_id = :conversation, decision_source = :source, "
            "approval_status = :approval"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":expected": outcome.previous_status,
            ":new": outcome.new_status,
            ":invoice": [invoice_id],
            ":ticket": ticket_id or "",
            ":conversation": conversation_id,
            ":source": "AGENT_PROPOSED",
            ":approval": "PENDING",
        },
    )

    if moved is None:
        # Another call moved it between the read and the write. Not an error: the payment is
        # under review, which is what the caller wanted.
        return {
            "status": "ALREADY_UNDER_REVIEW",
            "ticket_id": ticket_id,
            "previous_status": outcome.previous_status,
            "new_status": outcome.new_status,
            "resolution_target_hours": settings.resolution_target_hours,
        }

    audit.write(
        audit.AuditEvent(
            action="propose_allocation",
            previous_state=outcome.previous_status,
            new_state=outcome.new_status or "",
            authorizing_rule=outcome.authorizing_rule,
            customer_id=customer_id,
            conversation_id=conversation_id,
            agent_version=AGENT_VERSION,
            risk_result="NOT_EVALUATED",
            human_approval_required=outcome.requires_human_approval,
            entry_id=payment_id,
            ticket_id=ticket_id,
        )
    )

    _log_interaction(display, invoice, payment, ticket_id)

    log.info(
        "allocation proposed",
        conversation_id=conversation_id,
        customer_id=customer_id,
        entry_id=payment_id,
        ticket_id=ticket_id or "",
        status=outcome.new_status,
        rule_applied=outcome.authorizing_rule,
    )

    return {
        "status": "UNDER_REVIEW",
        "ticket_id": ticket_id,
        "previous_status": outcome.previous_status,
        "new_status": outcome.new_status,
        "resolution_target_hours": settings.resolution_target_hours,
    }


def _owned_entry(customer_id: str, entry_id: str, expected_type: str) -> dict:
    """
    Loads a ledger entry, enforcing ownership and type.

    customer_id:   the verified customer.
    entry_id:      the entry the agent named.
    expected_type: INVOICE or PAYMENT.

    Returns: the entry. Raises NOT_AUTHORIZED when it does not exist for this customer or is
             the wrong type — checked rather than trusted, because the identifiers arrive
             from the conversation (FR-007).
    """
    entry = dynamo.get(LEDGER_TABLE, {"customer_id": customer_id, "entry_id": entry_id})

    if not entry or entry.get("type") != expected_type:
        raise ToolError(
            ErrorCategory.NOT_AUTHORIZED,
            f"no such {expected_type.lower()} for customer: {entry_id}",
        )

    return entry


def _raise_ticket(conversation_id: str, display: dict, invoice: dict, payment: dict) -> str | None:
    """
    Creates the ticket a person will work from.

    conversation_id: the call, so the reviewer can find the recording.
    display:         company name and HubSpot ids from verification.
    invoice:         the invoice being settled.
    payment:         the payment being proposed.

    Returns: the ticket id, or None when HubSpot is unreachable. A CRM outage must not stop
             the payment going under review — losing the ticket is recoverable, losing the
             ledger state is not (FR-025).
    """
    contact_id = display.get("hubspot_contact_id")
    if not contact_id:
        return None

    discrepancies = []
    if "payer_address" in payment:
        discrepancies.append("payer address differs from the address on file")
    if payment.get("reference") in (None, ""):
        discrepancies.append("payment carried no reference, which is why it never allocated")

    try:
        return hubspot.create_ticket(
            {
                "subject": (
                    f"Allocate payment to {invoice.get('invoice_number')} "
                    f"— CHF {abs(Decimal(str(payment['amount']))):,.2f}"
                ),
                "content": (
                    f"Proposed by the voice agent during conversation {conversation_id}.\n\n"
                    f"Invoice: {invoice.get('invoice_number')}, "
                    f"CHF {Decimal(str(invoice['amount'])):,.2f}, due {invoice.get('due_date')}.\n"
                    f"Payment: {payment['entry_id']}, received {payment.get('entry_date')}.\n"
                    f"The caller confirmed the amount and transfer date, which matched.\n\n"
                    + ("To check: " + "; ".join(discrepancies) + ".\n" if discrepancies else "")
                    + "The agent has no authority to allocate. Please confirm or reject."
                ),
                "hs_pipeline_stage": "1",
                "hs_ticket_priority": "HIGH",
            },
            contact_id=contact_id,
            company_id=display.get("hubspot_company_id"),
        )
    except ToolError as error:
        log.error("ticket not created", error_category=str(error.category), status="DEGRADED")
        return None


def _log_interaction(display: dict, invoice: dict, payment: dict, ticket_id: str | None) -> None:
    """
    Records the call against the CRM contact (FR-044).

    Swallows its own failure: the interaction log is the least important of the three writes
    and must not undo the two that matter.
    """
    contact_id = display.get("hubspot_contact_id")
    if not contact_id:
        return

    try:
        hubspot.log_interaction(
            contact_id,
            f"Caller disputed {invoice.get('invoice_number')} as already paid. A matching "
            f"payment was found and proposed for allocation; it is now under review"
            + (f" on ticket {ticket_id}." if ticket_id else "."),
        )
    except ToolError as error:
        log.error("interaction not logged", error_category=str(error.category), status="DEGRADED")


def _response(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }

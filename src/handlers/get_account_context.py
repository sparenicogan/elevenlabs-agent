"""The get_account_context tool endpoint.

Everything the agent knows about a caller arrives through here, and it arrives only after
the backend says the caller is verified. It is the first tool that reads financial data, so
it is the first place the disclosure gate has something to protect.

Deliberately partial-tolerant: the ledger is the authority for money and HubSpot is not, so
a CRM outage degrades the response rather than failing the call (FR-025). A caller does not
need to be told their invoice is unavailable because a CRM is down.
"""

import json
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key

from src.adapters import dynamo, hubspot, secrets
from src.adapters.errors import ErrorCategory, ToolError
from src.common import auth, conversation_state, http
from src.common import logging as log
from src.domain import policy as policy_module

LEDGER_TABLE = "ledger"
SUMMARY_TABLE = "customer-summaries"

# Statuses that mean the customer still owes something.
OPEN_STATUSES = ("OPEN", "OVERDUE")

# The agent reads these aloud. A list long enough to lose track of is worse than a short one
# plus "there are others" — and a caller with forty open invoices is a collections problem,
# not a phone call.
MAX_INVOICES = 8


def handler(event: dict, _context: Any = None) -> dict:
    """
    Returns everything the agent may know about a verified caller's account.

    event: API Gateway proxy event carrying conversation_id.

    Returns: an API Gateway response with open invoices, escalations, CRM details and the
             bounded conversation summary. Refuses with NOT_AUTHORIZED unless the backend
             has recorded this conversation as verified (FR-001).
    """
    try:
        body = json.loads(event.get("body") or "{}")
        auth.require_api_key(event.get("headers") or {}, secrets.get("tools/api-key"))

        conversation_id = body.get("conversation_id")
        if not conversation_id:
            raise ToolError(ErrorCategory.VALIDATION, "missing conversation_id")

        # The gate. Raises NOT_AUTHORIZED unless the backend recorded VERIFIED — the
        # conversation's own claim to be verified is not consulted.
        customer_id, display = conversation_state.verified_context(conversation_id)

        return http.respond(200, _context_for(conversation_id, customer_id, display))

    except ToolError as error:
        return http.failed("get_account_context", error)


def _context_for(conversation_id: str, customer_id: str, display: dict) -> dict:
    """
    Assembles the account context for a verified customer.

    conversation_id: the call in progress, for correlation in the logs.
    customer_id:     resolved by verification, never supplied by the caller.
    display:         company name, language, status and HubSpot ids, recorded on the
                     conversation when verification succeeded. This handler holds no
                     permission on the identity table and needs none.

    Returns: the response body from contracts/tools.md. Financial fields come from the
             ledger and are always present; CRM fields come from HubSpot and may be absent.
    """
    settings = policy_module.load()
    invoices, recent = _invoices(customer_id)
    crm, escalations = _crm_context(display)

    log.info(
        "account context loaded",
        conversation_id=conversation_id,
        customer_id=customer_id,
        status="OK",
    )

    return {
        "status": "OK",
        "customer": {
            "company_name": display.get("company_name"),
            "preferred_language": display.get("preferred_language"),
            "account_status": display.get("account_status"),
        },
        "open_invoices": invoices[:MAX_INVOICES],
        "open_invoice_count": len(invoices),
        # Settled invoices too, newest first. A caller disputing a line on something they have
        # already paid is the ordinary case for a credit, and with only open invoices the
        # agent had no charge to attach one to and escalated a request it could have handled.
        "recent_invoices": recent[:MAX_INVOICES],
        "total_outstanding": _total(invoices),
        "open_escalations": [t for t in escalations if t.get("open")],
        "past_escalations": [t for t in escalations if not t.get("open")],
        "crm": crm,
        "summary_text": _summary(customer_id, settings.summary_max_chars),
    }


def _invoices(customer_id: str) -> tuple[list[dict], list[dict]]:
    """
    Reads the customer's invoices.

    customer_id: the verified customer.

    Returns: (open or overdue, oldest due date first) and (settled, newest issued first).
             Both carry the identifiers the agent needs to discuss an invoice and the numbers
             it may read aloud. A failure raises rather than returning empty lists — an empty
             list must mean 'nothing owed', never 'could not tell' (Principle III).
    """
    entries = dynamo.query(
        LEDGER_TABLE,
        index="status-index",
        KeyConditionExpression=Key("customer_id").eq(customer_id),
    )

    def shape(e: dict) -> dict:
        return {
            "entry_id": e["entry_id"],
            "invoice_number": e.get("invoice_number"),
            "amount": float(e["amount"]),
            "currency": e.get("currency", "CHF"),
            "issued_date": e.get("entry_date"),
            "due_date": e.get("due_date"),
            "status": e["status"],
        }

    all_invoices = [shape(e) for e in entries if e.get("type") == "INVOICE"]
    outstanding = [i for i in all_invoices if i["status"] in OPEN_STATUSES]
    settled = [i for i in all_invoices if i["status"] not in OPEN_STATUSES]

    return (
        sorted(outstanding, key=lambda i: i.get("due_date") or ""),
        sorted(settled, key=lambda i: i.get("issued_date") or "", reverse=True),
    )


def _total(invoices: list[dict]) -> float:
    """Sums what is outstanding. Derived from the entries, never stored (FR-017)."""
    return float(sum(Decimal(str(i["amount"])) for i in invoices))


def _crm_context(display: dict) -> tuple[dict | None, list[dict]]:
    """
    Reads the customer's CRM record and tickets.

    display: the fields verification recorded, which include the HubSpot ids.

    Returns: (crm fields, tickets). Both degrade to (None, []) when HubSpot is unreachable,
             because the CRM is not the authority for anything the caller is asking about
             and losing it must not cost them the call (FR-025).
    """
    contact_id = display.get("hubspot_contact_id")
    if not contact_id:
        return None, []

    try:
        contact = hubspot.get_contact(contact_id)
        tickets = hubspot.get_open_tickets(contact_id)
    except ToolError as error:
        # Logged and swallowed on purpose. The financial answer is already in hand.
        log.error("crm unavailable", error_category=str(error.category), status="DEGRADED")
        return None, []

    return (
        {"contact_id": contact_id, "company_id": display.get("hubspot_company_id"), **contact},
        [
            {
                "ticket_id": t["id"],
                "subject": t.get("subject"),
                "open": t.get("hs_pipeline_stage") not in ("4", "closed"),
            }
            for t in tickets
        ],
    )


def _summary(customer_id: str, max_chars: int) -> str | None:
    """
    Reads the bounded per-customer summary written after previous calls.

    customer_id: the verified customer.
    max_chars:   the cap from policy, enforced on read as well as on write.

    Returns: the summary, or None when the customer has no previous call. Narrative and
             preferences only: no financial statement may rest on it (FR-039c).
    """
    record = dynamo.get(SUMMARY_TABLE, {"customer_id": customer_id})
    if not record:
        return None
    return str(record.get("summary_text", ""))[:max_chars] or None

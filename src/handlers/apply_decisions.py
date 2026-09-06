"""Turns accepted requests into ledger entries.

The only component permitted to write the financial ledger, and the only one a caller cannot
reach: it runs on a schedule, behind its own role, with no API Gateway route.

It re-runs the credit rules rather than trusting the ticket. That is the point of the split.
The agent proposes and a person accepts, but neither is asked to do arithmetic — so a
compromised agent, or a colleague clicking Accepted on something they misread, still cannot
produce a ledger entry the rules would have refused.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key

from src.adapters import dynamo, hubspot
from src.adapters.errors import ToolError
from src.common import audit
from src.common import logging as log
from src.domain import policy as policy_module
from src.domain.credit import (
    CreditOutcome,
    entry_from_ledger,
    evaluate_credit,
    rolling_credit_total,
)
from src.domain.risk import RiskLevel

LEDGER_TABLE = "ledger"
AGENT_VERSION = "1.0.0"

# How far back to look for tickets someone has answered. Generous, because re-applying is a
# no-op and missing a decision is not.
LOOKBACK_DAYS = 7


def handler(_event: dict | None = None, _context: Any = None) -> dict:
    """
    Applies every accepted credit request that has not already reached the ledger.

    Returns: counts of what happened, for the schedule's logs.

    One ticket failing does not stop the rest: a malformed request should not hold up an
    unrelated customer's credit.
    """
    since = (datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    counts = {"applied": 0, "already_applied": 0, "refused": 0, "failed": 0}

    for ticket in hubspot.get_accepted_requests(since):
        try:
            counts[_apply(ticket)] += 1
        except (ToolError, KeyError, ValueError) as error:
            counts["failed"] += 1
            log.error("could not apply ticket", ticket_id=ticket.get("id"), error_detail=str(error))

    log.info("apply run complete", **{k: str(v) for k, v in counts.items()})
    return counts


def _apply(ticket: dict) -> str:
    """
    Applies one accepted ticket.

    ticket: an accepted request, with aws_customer_id, related_entry_id and credit_amount.

    Returns: which counter to increment — "applied", "already_applied" or "refused".
    """
    # A credit carries an amount; an allocation does not. The two are told apart by that
    # rather than by a type field, because the amount is what the credit rules need anyway.
    if ticket.get("credit_amount") in (None, ""):
        return _allocate(ticket)

    ticket_id = str(ticket["id"])
    customer_id = str(ticket["aws_customer_id"])
    entry_id = str(ticket["related_entry_id"])
    amount = Decimal(str(ticket["credit_amount"]))

    decision = _revalidate(customer_id, entry_id, amount)
    if decision.outcome is not CreditOutcome.GRANTED:
        # Accepted by a person, refused by the rules. The rules win, and the refusal is
        # written where somebody will read it rather than swallowed.
        hubspot.append_note(
            ticket_id,
            f"Not applied. Re-checking against the ledger refused it: {decision.authorizing_rule}. "
            "Nothing has been credited.",
        )
        log.info(
            "accepted ticket refused on recheck",
            ticket_id=ticket_id,
            rule_applied=decision.authorizing_rule,
        )
        return "refused"

    written = dynamo.put_if_absent(
        LEDGER_TABLE,
        {
            "customer_id": customer_id,
            # Derived from the ticket, so a second run writes nothing rather than a second
            # credit. This is the whole of the applier's idempotency.
            "entry_id": f"cn_{ticket_id}",
            "type": "CREDIT_NOTE",
            # Negative, because a credit reduces what is owed.
            "amount": -decision.credit_amount,
            "currency": "CHF",
            "entry_date": datetime.now(UTC).date().isoformat(),
            "status": "APPROVED",
            "allocated_to": [entry_id],
            "source_ticket_id": ticket_id,
            "decision_source": "HUMAN_ACCEPTED",
            "approval_status": "APPROVED",
        },
        key_field="entry_id",
    )

    if not written:
        return "already_applied"

    audit.write(
        audit.AuditEvent(
            action="apply_credit",
            previous_state="REQUESTED",
            new_state="APPROVED",
            authorizing_rule=decision.authorizing_rule,
            customer_id=customer_id,
            conversation_id="",
            agent_version=AGENT_VERSION,
            risk_result="NOT_EVALUATED",
            # A person accepted it and the rules were re-checked against the ledger. Both.
            human_approval_required=True,
            entry_id=f"cn_{ticket_id}",
            ticket_id=ticket_id,
        )
    )
    hubspot.append_note(ticket_id, f"Applied to the ledger as cn_{ticket_id}.")
    log.info(
        "credit applied", ticket_id=ticket_id, customer_id=customer_id, entry_id=f"cn_{ticket_id}"
    )
    return "applied"


def _allocate(ticket: dict) -> str:
    """
    Moves an accepted payment against the invoices it settles.

    ticket: an accepted allocation, whose related_entry_id lists the payment first and then
            the invoices it covers.

    Returns: which counter to increment.

    The payment is re-read and the arithmetic re-checked. A person accepting a ticket says they
    are content for it to happen, not that the numbers add up — and by the time they look, the
    invoices may have been settled some other way.
    """
    ticket_id = str(ticket["id"])
    customer_id = str(ticket["aws_customer_id"])
    entries = [e.strip() for e in str(ticket["related_entry_id"]).split(",") if e.strip()]
    payment_id, invoice_ids = entries[0], entries[1:]

    if not invoice_ids:
        raise ValueError(f"ticket {ticket_id} names no invoice to allocate against")

    ledger = dynamo.query(LEDGER_TABLE, KeyConditionExpression=Key("customer_id").eq(customer_id))
    by_id = {str(e["entry_id"]): e for e in ledger}
    payment = by_id.get(payment_id)
    invoices = [by_id[i] for i in invoice_ids if i in by_id]

    if not payment or len(invoices) != len(invoice_ids):
        return _refuse(ticket_id, "the payment or an invoice is no longer on the account")

    covered = sum(abs(Decimal(str(i["amount"]))) for i in invoices)
    if abs(Decimal(str(payment["amount"]))) < covered:
        return _refuse(ticket_id, "the payment does not cover the invoices named on it")

    moved = dynamo.update_if(
        LEDGER_TABLE,
        {"customer_id": customer_id, "entry_id": payment_id},
        # Only from UNALLOCATED, so a second run and a concurrent applier both write once.
        condition="#s = :expected",
        UpdateExpression=(
            "SET #s = :new, allocated_to = :invoices, source_ticket_id = :ticket, "
            "decision_source = :source, approval_status = :approval"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":expected": "UNALLOCATED",
            ":new": "ALLOCATED",
            ":invoices": invoice_ids,
            ":ticket": ticket_id,
            ":source": "HUMAN_ACCEPTED",
            ":approval": "APPROVED",
        },
    )

    if moved is None:
        return "already_applied"

    # The invoices the payment covers are settled in the same run. Without this the payment
    # reads ALLOCATED while the invoice it paid still reads OVERDUE, so the next caller is
    # told the thing they rang about last week is still outstanding -- which is the one
    # outcome the whole journey exists to prevent. Conditional on not already being PAID, so
    # a repeat run and a concurrent applier both write once.
    for invoice_id in invoice_ids:
        dynamo.update_if(
            LEDGER_TABLE,
            {"customer_id": customer_id, "entry_id": invoice_id},
            condition="#s <> :paid",
            UpdateExpression=(
                "SET #s = :paid, settled_by_entry_id = :payment, source_ticket_id = :ticket, "
                "decision_source = :source"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":paid": "PAID",
                ":payment": payment_id,
                ":ticket": ticket_id,
                ":source": "HUMAN_ACCEPTED",
            },
        )

    audit.write(
        audit.AuditEvent(
            action="apply_allocation",
            previous_state="UNALLOCATED",
            new_state="ALLOCATED",
            authorizing_rule="human_accepted_allocation",
            customer_id=customer_id,
            conversation_id="",
            agent_version=AGENT_VERSION,
            risk_result="NOT_EVALUATED",
            human_approval_required=True,
            entry_id=payment_id,
            ticket_id=ticket_id,
        )
    )
    hubspot.append_note(ticket_id, f"Allocated {payment_id} to {', '.join(invoice_ids)}.")
    log.info(
        "allocation applied", ticket_id=ticket_id, customer_id=customer_id, entry_id=payment_id
    )
    return "applied"


def _refuse(ticket_id: str, why: str) -> str:
    """Writes the refusal where a person will read it, and applies nothing."""
    hubspot.append_note(ticket_id, f"Not applied. {why[0].upper()}{why[1:]}. Nothing was changed.")
    log.info("accepted ticket refused on recheck", ticket_id=ticket_id, rule_applied=why)
    return "refused"


def _revalidate(customer_id: str, entry_id: str, amount: Decimal):
    """
    Re-runs the credit rules against the ledger as it stands now.

    customer_id: the company the charge belongs to.
    entry_id:    the charge the credit attaches to.
    amount:      what the ticket asks for.

    Returns: a CreditDecision.

    Deliberately reads the ledger again rather than trusting anything on the ticket. Time has
    passed since the caller rang: other credits may have landed, the charge may have been
    settled, and the ceilings are measured against today.
    """
    settings = policy_module.load()
    ledger = dynamo.query(LEDGER_TABLE, KeyConditionExpression=Key("customer_id").eq(customer_id))
    credits = [e for e in ledger if e.get("type") == "CREDIT_NOTE"]
    entry = next((e for e in ledger if str(e.get("entry_id")) == entry_id), None)

    return evaluate_credit(
        requested_amount=amount,
        entry=entry_from_ledger(entry, credits),
        account_status="ACTIVE",
        rolling_total=rolling_credit_total(credits, date.today(), settings.credit_window_months),
        # Risk was evaluated on the call, and a person has accepted it since. What this
        # re-check is for is the arithmetic, which cannot go stale in the caller's favour.
        risk_level=RiskLevel.NONE,
        max_per_request=settings.credit_max_per_request,
        max_rolling=settings.credit_max_rolling,
    )

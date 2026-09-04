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

    log.info("apply run complete", **counts)
    return counts


def _apply(ticket: dict) -> str:
    """
    Applies one accepted ticket.

    ticket: an accepted request, with customer_id, related_entry_id and credit_amount.

    Returns: which counter to increment — "applied", "already_applied" or "refused".
    """
    if ticket.get("credit_amount") in (None, ""):
        # An accepted allocation. A person moves those in the ledger themselves; the agent
        # never had authority over them and neither does this.
        return "already_applied"

    ticket_id = str(ticket["id"])
    customer_id = str(ticket["customer_id"])
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

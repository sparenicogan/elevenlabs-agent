"""The match_payment tool endpoint.

The centre of the golden path. A caller says they paid; this decides whether the payment
they describe is one the company actually holds, without telling them anything about what
is on record.

The asymmetry is deliberate and is the whole security property: the caller supplies values
and learns only whether they matched. Nothing flows the other way. A caller who has not
demonstrated knowledge of a payment learns nothing from trying — not the amount, not the
date, not even that a payment exists.
"""

import json
from datetime import date
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key

from src.adapters import dynamo, secrets
from src.adapters.errors import ErrorCategory, ToolError
from src.common import auth, conversation_state, http, validation
from src.common import logging as log
from src.domain import policy as policy_module
from src.domain.payment_match import (
    MatchStatus,
    PaymentCandidate,
    ReferenceLink,
    match_payment,
)

LEDGER_TABLE = "ledger"

# A payment can only be claimed against an invoice that is still owed. Matching against a
# settled invoice would be pointless at best and, if the agent then proposed an allocation,
# would double-allocate a payment.
CLAIMABLE_INVOICE_STATUSES = ("OPEN", "OVERDUE")


class _ReferenceResolver:
    """
    Answers one question: does this string resolve to a real invoice?

    That question separates a mistyped reference from a payment earmarked for a different
    invoice (FR-010g), and it must span customers — a payment quoting another customer's
    invoice belongs to them, not to the caller on the line.

    Implemented as a container rather than a set because the alternative is loading every
    invoice in the system to answer a question asked at most once per call. Each lookup is a
    single indexed query.
    """

    def __init__(self, exclude_entry_id: str):
        # The invoice under discussion resolves to itself, which would make its own
        # reference look like another invoice's. Excluded by entry id rather than by value,
        # so both of its identifiers are covered.
        self._exclude = exclude_entry_id
        self._cache: dict[str, bool] = {}

    def __contains__(self, candidate: str) -> bool:
        if candidate in self._cache:
            return self._cache[candidate]

        resolved = self._resolves(candidate)
        self._cache[candidate] = resolved
        return resolved

    def _resolves(self, candidate: str) -> bool:
        """Queries both identifier indexes. Either hit means a real invoice."""
        for index, attribute in (
            ("reference-index", "payment_reference"),
            ("invoice-number-index", "invoice_number"),
        ):
            hits = dynamo.query(
                LEDGER_TABLE,
                index=index,
                KeyConditionExpression=Key(attribute).eq(candidate),
            )
            if any(hit["entry_id"] != self._exclude for hit in hits):
                return True
        return False


def handler(event: dict, _context: Any = None) -> dict:
    """
    Compares a payment the caller describes against the payments on record.

    event: API Gateway proxy event carrying conversation_id, invoice_entry_id, and the
           caller's claimed_amount and claimed_transfer_date.

    Returns: an API Gateway response carrying MATCH, NO_MATCH or INSUFFICIENT and, on a
             match, the payment's identifier and the flags a human will need. Never a stored
             amount, date, reference or address (FR-010).
    """
    try:
        body = json.loads(event.get("body") or "{}")
        auth.require_api_key(event.get("headers") or {}, secrets.get("tools/api-key"))

        conversation_id = body.get("conversation_id")
        if not conversation_id:
            raise ToolError(ErrorCategory.VALIDATION, "missing conversation_id")

        customer_id, _ = conversation_state.verified_context(conversation_id)
        return http.respond(200, _match(conversation_id, customer_id, body))

    except ToolError as error:
        return http.failed("match_payment", error)


def _match(conversation_id: str, customer_id: str, body: dict) -> dict:
    """
    Runs the matching rule against the customer's payments.

    conversation_id: the call in progress.
    customer_id:     from verification, never from the request.
    body:            the parsed request.

    Returns: the response body from contracts/tools.md.
    """
    settings = policy_module.load()
    invoice_entry_id = str(validation.require(body, "invoice_entry_id"))
    invoice = _invoice_for(customer_id, invoice_entry_id)

    claimed_amount = _optional_amount(body)
    claimed_date = _optional_date(body)

    result = match_payment(
        claimed_amount=claimed_amount,
        claimed_transfer_date=claimed_date,
        invoice_amount=Decimal(str(invoice["amount"])),
        invoice_number=str(invoice.get("invoice_number", "")),
        invoice_payment_reference=str(invoice.get("payment_reference", "")),
        candidates=_candidates(customer_id),
        known_references=_ReferenceResolver(exclude_entry_id=invoice_entry_id),
        tolerance_days=settings.payment_date_tolerance_days,
        typo_distance=settings.reference_typo_max_distance,
    )

    log.info(
        "payment matched",
        conversation_id=conversation_id,
        customer_id=customer_id,
        entry_id=invoice_entry_id,
        status=str(result.status),
    )

    return _body(result, _payer_address(_ledger_payments(customer_id), result.payment_entry_id))


def _invoice_for(customer_id: str, entry_id: str) -> dict:
    """
    Loads the invoice under discussion, enforcing ownership.

    customer_id: the verified customer.
    entry_id:    the invoice the agent named.

    Returns: the ledger entry. Raises NOT_AUTHORIZED if it belongs to someone else or is not
             an invoice, and VALIDATION if it is already settled.

    Ownership is checked here rather than trusted because the entry id arrives from the
    conversation, and a model that has seen one customer's invoice id could repeat it in
    another call (FR-007).
    """
    entry = dynamo.get(LEDGER_TABLE, {"customer_id": customer_id, "entry_id": entry_id})

    if not entry or entry.get("type") != "INVOICE":
        raise ToolError(ErrorCategory.NOT_AUTHORIZED, f"no such invoice for customer: {entry_id}")

    if entry.get("status") not in CLAIMABLE_INVOICE_STATUSES:
        raise ToolError(ErrorCategory.VALIDATION, f"invoice is not open: {entry.get('status')}")

    return entry


def _ledger_payments(customer_id: str) -> list[dict]:
    """The customer's unallocated payment rows, as stored."""
    return [
        entry
        for entry in dynamo.query(
            LEDGER_TABLE,
            index="status-index",
            KeyConditionExpression=Key("customer_id").eq(customer_id)
            & Key("status").eq("UNALLOCATED"),
        )
        if entry.get("type") == "PAYMENT"
    ]


def _candidates(customer_id: str) -> list[PaymentCandidate]:
    """
    Loads the customer's payments that could plausibly settle an invoice.

    customer_id: the verified customer.

    Returns: one PaymentCandidate per unallocated payment, carrying booleans rather than the
             payer's name and address so the matching rule cannot leak either.
    """
    entries = dynamo.query(
        LEDGER_TABLE,
        index="status-index",
        KeyConditionExpression=Key("customer_id").eq(customer_id) & Key("status").eq("UNALLOCATED"),
    )

    return [
        PaymentCandidate(
            entry_id=entry["entry_id"],
            # Payments are stored signed negative; the caller states a positive amount.
            amount=abs(Decimal(str(entry["amount"]))),
            execution_date=date.fromisoformat(str(entry["entry_date"])),
            status=str(entry["status"]),
            reference=entry.get("reference"),
            payer_name_matches=entry.get("payer_name_mismatch") is not True,
            # A payer address is only recorded when it differs from the customer's, so its
            # presence is the mismatch. The value itself never leaves the ledger.
            payer_address_matches="payer_address" not in entry,
        )
        for entry in entries
        if entry.get("type") == "PAYMENT"
    ]


def _payer_address(entries: list[dict], entry_id: str | None) -> str | None:
    """
    The address recorded against the matched payment, as one spoken line.

    entries:  the customer's ledger rows.
    entry_id: the matched payment, or None when nothing matched.

    Returns: "street, postcode city", or None when the row carries no address. Never the
             address on file: the caller is being asked about the one on the payment, and the
             other adds nothing they do not already know.
    """
    entry = next((e for e in entries if e.get("entry_id") == entry_id), None)
    address = (entry or {}).get("payer_address")
    if not address:
        return None
    street, postcode, city = (
        address.get("street", ""),
        address.get("postcode", ""),
        address.get("city", ""),
    )
    return f"{street}, {postcode} {city}".strip(", ")


def _optional_amount(body: dict) -> Decimal | None:
    """The claimed amount, or None when the caller could not supply it — which is
    INSUFFICIENT rather than a validation error, because not knowing is a normal answer."""
    if body.get("claimed_amount") in (None, ""):
        return None
    return validation.money(body, "claimed_amount")


def _optional_date(body: dict) -> date | None:
    """The claimed transfer date, or None when the caller could not supply it."""
    if body.get("claimed_transfer_date") in (None, ""):
        return None
    return validation.iso_date(body, "claimed_transfer_date")


def _body(result, payer_address: str | None = None) -> dict:
    """
    Renders the result for the agent.

    result: the domain MatchResult.

    Returns: the contract's response. Fields that would describe a payment are present only
             on a MATCH, so a failed attempt reveals nothing about what is on record —
             including whether anything is.
    """
    body: dict[str, Any] = {"status": str(result.status)}

    if result.status is MatchStatus.INSUFFICIENT:
        body["missing_fields"] = list(result.missing_fields)
        return body

    if result.status is MatchStatus.NO_MATCH:
        # The one exception, and it is not a leak: the caller stated the amount and date
        # correctly, so they already know the payment exists. Saying it is earmarked
        # elsewhere is what lets the agent explain rather than stonewall.
        if result.reference_link is ReferenceLink.OTHER_INVOICE:
            body["reference_link"] = str(result.reference_link)
        return body

    body.update(
        {
            "payment_entry_id": result.payment_entry_id,
            "covers_invoice": result.covers_invoice,
            "requires_human_allocation": True,
            "reference_link": str(result.reference_link),
            "address_discrepancy": result.address_discrepancy,
            # The address the payment carried, so the caller can say whether it is theirs.
            # Only on a MATCH, so only to someone whose payment details already checked out,
            # and only when it differs -- asking "was that a typo?" without saying what "that"
            # is asks the caller to confirm something they cannot see.
            # The address the payment carried, so the caller can say whether it is theirs.
            # Only on a MATCH, and only when it differs: asking "was that a typo?" without
            # saying what "that" is asks someone to confirm what they cannot see.
            "payer_address": payer_address,
            "payer_name_discrepancy": result.payer_name_discrepancy,
        }
    )
    return body

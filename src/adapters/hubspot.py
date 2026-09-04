"""HubSpot CRM access, with a field allowlist enforced at this boundary.

FR-028 lists what the CRM may hold. Enforcing it here rather than at each call site means
a future handler cannot leak a date of birth into a ticket by passing the wrong dict: the
allowlist strips it before the request is built.
"""

import httpx

from src.adapters import secrets
from src.adapters.errors import ErrorCategory, ToolError

_BASE_URL = "https://api.hubapi.com"
_TIMEOUT_SECONDS = 3.0

# Everything the CRM is permitted to hold. Anything else is dropped before the request,
# not merely omitted by convention.
ALLOWED_CONTACT_FIELDS = frozenset({"firstname", "lastname", "company", "hs_object_id", "email"})
ALLOWED_TICKET_FIELDS = frozenset(
    {
        "subject",
        "content",
        "hs_pipeline",
        "hs_pipeline_stage",
        "hs_ticket_priority",
        # The three custom properties that make a ticket a financial request rather than
        # prose. The ceiling is computed from credit_amount, so it must be a field the CRM
        # can filter and sum, never a number mentioned in the subject line.
        "request_outcome",
        "credit_amount",
        "related_entry_id",
        # The company the charge belongs to. The applier needs it to read the ledger, which
        # is keyed by customer: a ticket that cannot name its customer cannot be applied.
        "customer_id",
    }
)

# A request is pending until a person answers it. request_outcome is required to close a
# ticket, so its absence is what "still open" means here — more reliable than a pipeline
# stage id, which is configurable per account and would silently drift.
_UNDECIDED = "NOT_HAS_PROPERTY"


def _strip(properties: dict, allowed: frozenset[str]) -> dict:
    """Drops any property outside the allowlist. The enforcement point for FR-028."""
    return {k: v for k, v in properties.items() if k in allowed}


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    """
    Makes one authenticated HubSpot call.

    method:  HTTP verb.
    path:    path below the API base, e.g. "/crm/v3/objects/tickets".
    payload: JSON body, already stripped to its allowlist by the caller.

    Returns: the decoded response. Raises ToolError(DEPENDENCY_DOWN) on transport failure
             or a non-2xx response, so a CRM outage never looks like a successful write.
    """
    token = secrets.get("hubspot/private-app-token")

    try:
        response = httpx.request(
            method,
            f"{_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise ToolError(ErrorCategory.DEPENDENCY_DOWN, f"hubspot {path}: {exc}") from exc


def create_ticket(properties: dict, contact_id: str, company_id: str | None = None) -> str:
    """
    Creates a ticket and associates it with the contact and company.

    properties: ticket fields. Anything outside ALLOWED_TICKET_FIELDS is discarded.
    contact_id: HubSpot contact to associate the ticket with.
    company_id: HubSpot company, when known.

    Returns: the new ticket id.
    """
    associations = [
        {
            "to": {"id": contact_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 16}],
        }
    ]
    if company_id:
        associations.append(
            {
                "to": {"id": company_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 26}],
            }
        )

    result = _request(
        "POST",
        "/crm/v3/objects/tickets",
        {"properties": _strip(properties, ALLOWED_TICKET_FIELDS), "associations": associations},
    )
    return result["id"]


def create_unassociated_ticket(properties: dict) -> str:
    """
    Creates a ticket linked to no contact and no company.

    properties: ticket fields, stripped to the allowlist.

    Returns: the new ticket id.

    Used when the caller could not be verified. Associating a ticket on an unverified
    caller's claim would write an unverified identity into the CRM, which is precisely what
    the data boundary exists to prevent (FR-019d). It goes to a queue instead, and the human
    establishes who they were speaking to.
    """
    result = _request(
        "POST",
        "/crm/v3/objects/tickets",
        {"properties": _strip(properties, ALLOWED_TICKET_FIELDS)},
    )
    return result["id"]


def append_note(ticket_id: str, body: str) -> None:
    """
    Adds a note to an existing ticket, so a second finding on one call does not create a
    second ticket (FR-031b).

    ticket_id: the ticket to append to.
    body:      the note text. Must already be free of identity and payment detail.

    Returns: nothing.
    """
    note = _request("POST", "/crm/v3/objects/notes", {"properties": {"hs_note_body": body}})
    _request(
        "PUT",
        f"/crm/v3/objects/notes/{note['id']}/associations/tickets/{ticket_id}/note_to_ticket",
    )


def log_interaction(contact_id: str, body: str) -> None:
    """
    Records that a call happened and what came of it (FR-044).

    contact_id: the HubSpot contact.
    body:       a summary carrying outcome and ticket references, never financial detail
                beyond what a ticket needs.

    Returns: nothing.
    """
    engagement = _request("POST", "/crm/v3/objects/notes", {"properties": {"hs_note_body": body}})
    _request(
        "PUT",
        f"/crm/v3/objects/notes/{engagement['id']}"
        f"/associations/contacts/{contact_id}/note_to_contact",
    )


def get_contact(contact_id: str) -> dict:
    """
    Reads a CRM contact for the account context a verified caller is entitled to.

    contact_id: HubSpot contact id, stored on the backend customer record.

    Returns: the allowlisted properties only. HubSpot may hold fields this project never
             wrote; stripping on read as well as write means a stray field added in the
             HubSpot UI cannot reach a transcript or a log.
    """
    result = _request(
        "GET",
        f"/crm/v3/objects/contacts/{contact_id}"
        f"?properties={','.join(sorted(ALLOWED_CONTACT_FIELDS))}",
    )
    return _strip(result.get("properties", {}), ALLOWED_CONTACT_FIELDS)


def _search_tickets(association: str, object_id: str, undecided_only: bool) -> list[dict]:
    """
    Finds tickets associated with one CRM object.

    association:    "contact" or "company", the object type the ticket hangs off.
    object_id:      that object's HubSpot id.
    undecided_only: when true, returns only tickets no one has answered yet.

    Returns: one dict per ticket with its id and allowlisted properties. Empty means "none";
             a failure raises, because a read that could not be completed must never be
             mistaken for a customer with nothing outstanding.

    One search call rather than an association lookup followed by a GET per ticket. The
    fan-out mattered: this runs on every verified call, and the account is rate limited
    across all private apps.
    """
    filters = [
        {"propertyName": f"associations.{association}", "operator": "EQ", "value": object_id}
    ]
    if undecided_only:
        filters.append({"propertyName": "request_outcome", "operator": _UNDECIDED})

    result = _request(
        "POST",
        "/crm/v3/objects/tickets/search",
        {
            "filterGroups": [{"filters": filters}],
            "properties": sorted(ALLOWED_TICKET_FIELDS),
            "limit": 100,
        },
    )

    return [
        {"id": ticket["id"], **_strip(ticket.get("properties") or {}, ALLOWED_TICKET_FIELDS)}
        for ticket in result.get("results", [])
    ]


def get_open_tickets(contact_id: str) -> list[dict]:
    """
    Lists tickets associated with a contact, for the open and past escalations in the
    account context (FR-039b).

    contact_id: HubSpot contact id.

    Returns: one dict per ticket with its id and allowlisted properties.
    """
    return _search_tickets("contact", contact_id, undecided_only=False)


def get_accepted_requests(modified_since: str) -> list[dict]:
    """
    Lists requests a person has accepted, for the applier to act on.

    modified_since: ISO timestamp; tickets untouched since then are not revisited.

    Returns: one dict per accepted ticket with its id and allowlisted properties.

    Bounded by modification date only to keep the search small. Correctness does not depend
    on it: applying is idempotent, because the ledger entry id is derived from the ticket id
    and the conditional write refuses a second one.
    """
    result = _request(
        "POST",
        "/crm/v3/objects/tickets/search",
        {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "request_outcome", "operator": "EQ", "value": "Accepted"},
                        {
                            "propertyName": "hs_lastmodifieddate",
                            "operator": "GTE",
                            "value": modified_since,
                        },
                    ]
                }
            ],
            "properties": sorted(ALLOWED_TICKET_FIELDS),
            "limit": 100,
        },
    )

    return [
        {"id": ticket["id"], **_strip(ticket.get("properties") or {}, ALLOWED_TICKET_FIELDS)}
        for ticket in result.get("results", [])
    ]


def get_pending_requests(company_id: str) -> list[dict]:
    """
    Lists the financial requests still awaiting a decision for a whole company.

    company_id: HubSpot company id.

    Returns: one dict per undecided ticket, with credit_amount and related_entry_id where
             the ticket carries them.

    Scoped to the company, not the caller, because the credit ceiling belongs to the
    customer: a colleague who rang this morning has already spent part of it, and a second
    caller must not be told a payment is unreviewed when a review is already open on it.
    """
    return _search_tickets("company", company_id, undecided_only=True)

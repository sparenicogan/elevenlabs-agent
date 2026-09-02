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
    {"subject", "content", "hs_pipeline", "hs_pipeline_stage", "hs_ticket_priority"}
)


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


def get_open_tickets(contact_id: str) -> list[dict]:
    """
    Lists tickets associated with a contact, for the open and past escalations in the
    account context (FR-039b).

    contact_id: HubSpot contact id.

    Returns: one dict per ticket with its id and allowlisted properties. Empty when there
             are none — an empty list means "none", never "could not tell", because a
             failure raises instead.
    """
    associations = _request("GET", f"/crm/v3/objects/contacts/{contact_id}/associations/tickets")

    tickets = []
    for association in associations.get("results", []):
        ticket = _request(
            "GET",
            f"/crm/v3/objects/tickets/{association['id']}"
            f"?properties={','.join(sorted(ALLOWED_TICKET_FIELDS))}",
        )
        tickets.append(
            {"id": ticket["id"], **_strip(ticket.get("properties", {}), ALLOWED_TICKET_FIELDS)}
        )
    return tickets

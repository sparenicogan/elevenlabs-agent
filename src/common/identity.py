"""Finding whose record to check.

Extracted so the per-factor check and the final verification resolve callers the same way.
When they were separate implementations they drifted, and the drift was a caller who could
recite their own phone number and never be found by it.
"""

from boto3.dynamodb.conditions import Key

from src.adapters import dynamo
from src.domain.verification import Factor, lookup_key

TABLE = "customer-identity"

# Which index answers which identifier. A date of birth is not here: it is checked against a
# record, never used to find one, because thousands of people share any given date.
INDEXES = {
    Factor.EMAIL: ("email-index", "email_lookup"),
    Factor.PHONE: ("phone-index", "phone_lookup"),
}


def lookup_contact(factor: Factor, value: str) -> str | None:
    """
    Finds the contact an identifier belongs to.

    factor: EMAIL or PHONE.
    value:  what the caller said, in any form.

    Returns: the contact id, or None when nothing matches or more than one does. Normalisation
             merges values that differ only by punctuation, so an ambiguous match resolves
             nobody rather than guessing between two people.
    """
    if factor not in INDEXES:
        return None

    index, attribute = INDEXES[factor]
    hits = dynamo.query(
        TABLE, index=index, KeyConditionExpression=Key(attribute).eq(lookup_key(factor, value))
    )
    # A row with no contact_id resolves nobody, the same as no row at all. An index whose
    # projection changes should degrade to "not found" rather than raise on a live call.
    return str(hits[0]["contact_id"]) if len(hits) == 1 and hits[0].get("contact_id") else None


def load_record(contact_id: str) -> dict | None:
    """Reads one identity record, or None when the contact does not exist."""
    return dynamo.get(TABLE, {"contact_id": contact_id})

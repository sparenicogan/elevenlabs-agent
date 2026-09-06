"""What a customer has done on previous calls, for the rules that decide about money.

Three fields: who called, when, and how it went. Nothing else, deliberately.

This is a control input, not a report. The credit rules read it to raise contact-frequency
and repeated-dispute signals, and a signal that fails to fire is a credit granted that should
have been reviewed -- so the less this table carries, the less can drift underneath a
decision. It is kept apart from the performance table for that reason: performance exists to
be reshaped, backfilled and migrated as monitoring questions change, and a financial control
must not move because somebody added a field to a dashboard.

Kept apart from the conversation record too, which holds live call state and expires within
days. This looks back a year.
"""

from datetime import UTC, datetime, timedelta

from boto3.dynamodb.conditions import Key

from src.adapters import dynamo

_TABLE = "call-history"

# A year and a quarter. The credit rules look back 365 days, and a record that expired inside
# that window would stop raising signals rather than fail -- the quiet kind of wrong.
RETENTION_DAYS = 450


def record(customer_id: str, conversation_id: str, started_at: str, outcome: str) -> None:
    """
    Notes that a call happened, once it has ended.

    customer_id:     the verified customer. A call nobody verified belongs to no customer and
                     is not recorded here -- there is no history to attach it to.
    conversation_id: the call.
    started_at:      when it began, ISO-8601. Also the sort key, so a customer's calls read
                     back newest first without an index.
    outcome:         one of the five in data-model.md.

    Returns: nothing.
    """
    if not customer_id:
        return

    dynamo.upsert(
        _TABLE,
        {"customer_id": customer_id, "started_at": started_at},
        UpdateExpression="SET conversation_id = :c, outcome = :o, expires_at = :e",
        ExpressionAttributeValues={
            ":c": conversation_id,
            ":o": outcome,
            ":e": int((datetime.now(UTC) + timedelta(days=RETENTION_DAYS)).timestamp()),
        },
    )


def recent(customer_id: str, limit: int = 50) -> list[dict]:
    """
    A customer's recent calls, newest first.

    customer_id: the verified customer.
    limit:       how many to read. Fifty is far more than any pattern needs and small enough
                 to stay a single query.

    Returns: records carrying started_at and outcome. Empty when the customer has never
             called before, which is the ordinary case for a new customer and must not look
             like a failure.
    """
    return dynamo.query(
        _TABLE,
        KeyConditionExpression=Key("customer_id").eq(customer_id),
        ScanIndexForward=False,
        Limit=limit,
    )

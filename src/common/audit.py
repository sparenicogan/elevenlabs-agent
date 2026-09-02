"""Audit events for every consequential automated action.

FR-041 fixes the field set, so it is built here rather than at each call site: an event
missing its authorizing rule or its previous state cannot be reconstructed later, and by
then the call is over.
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from src.common import logging as structured_log


@dataclass(frozen=True)
class AuditEvent:
    """
    One consequential action, in a form that can be replayed and defended a decade later.

    action:          what was done, e.g. "propose_allocation" or "issue_credit".
    previous_state:  the record's state before the action. "NONE" when it did not exist.
    new_state:       the record's state after.
    authorizing_rule: the named rule that permitted it, e.g. "credit_max_per_request".
    customer_id:     the customer the action applies to.
    entry_id:        the ledger entry acted on, where there is one.
    conversation_id: the call during which it happened.
    agent_version:   which agent version was live, so behaviour can be attributed after a change.
    risk_result:     the risk evaluation outcome at decision time.
    human_approval_required: whether the action still needs a person to confirm it.
    """

    action: str
    previous_state: str
    new_state: str
    authorizing_rule: str
    customer_id: str
    conversation_id: str
    agent_version: str
    risk_result: str
    human_approval_required: bool
    entry_id: str | None = None
    ticket_id: str | None = None


def write(event: AuditEvent) -> None:
    """
    Records one audit event to the audit log stream.

    event: a fully populated AuditEvent. Every field is required by FR-041; there is no
           partial audit.

    Returns: nothing. Failure to audit is itself logged but never blocks the action, which
             has already happened by the time this is called.
    """
    payload = asdict(event)
    payload["timestamp"] = datetime.now(UTC).isoformat()
    payload["event_type"] = "AUDIT"

    # Bypasses the allowlist in structured_log deliberately: an audit record's fields are
    # fixed by AuditEvent, contain no caller speech and no identity attributes, and must
    # not be silently trimmed.
    structured_log.configure()
    logging.getLogger("voice-agent").info(json.dumps(payload, default=str))

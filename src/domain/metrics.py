"""The per-interaction record, and the nine rates derived from it.

FR-042 lists what is stored; FR-043 lists what is computed. None of the rates is stored
precomputed, because a stored rate is a number that was true once and is asserted for ever.
Everything here is a pure function of records that already exist.
"""

from dataclasses import dataclass, field
from decimal import Decimal

# The outcomes that count as the agent having finished the job itself.
AUTONOMOUS_OUTCOMES = frozenset({"RESOLVED_AUTONOMOUS"})
ESCALATED_OUTCOMES = frozenset({"ESCALATED", "TRANSFERRED"})


@dataclass(frozen=True)
class Interaction:
    """
    One call, as stored on the conversation record.

    Only the fields the rates need. Everything else on the record is context for a person
    reading it afterwards.
    """

    conversation_id: str
    outcome: str
    duration_seconds: int = 0
    verification_result: str | None = None
    verification_attempts: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    credits_requested: int = 0
    credits_blocked: int = 0
    escalation_required: bool = False
    repeat_within_window: bool = False
    financial_actions: list[str] = field(default_factory=list)


def _rate(numerator: int, denominator: int) -> float:
    """A proportion, or zero when there is nothing to divide. Never a division by zero on a
    dashboard that is simply new."""
    return round(numerator / denominator, 4) if denominator else 0.0


def derive(interactions: list[Interaction]) -> dict[str, float]:
    """
    Computes the nine rates in FR-043.

    interactions: every call in the period.

    Returns: the rates by name, each a proportion between 0 and 1 except the average handling
             time, which is seconds.

    A first-call resolution is a call that resolved and was not followed by the same customer
    ringing again inside the window — which is why it is derived from the set rather than
    from any single record.
    """
    total = len(interactions)
    verified_attempts = [i for i in interactions if i.verification_result]

    return {
        "first_call_resolution": _rate(
            sum(
                1 for i in interactions if not i.repeat_within_window and not i.escalation_required
            ),
            total,
        ),
        "autonomous_resolution_rate": _rate(
            sum(1 for i in interactions if i.outcome in AUTONOMOUS_OUTCOMES), total
        ),
        "escalation_rate": _rate(
            sum(1 for i in interactions if i.outcome in ESCALATED_OUTCOMES), total
        ),
        "average_handling_time_seconds": round(
            sum(i.duration_seconds for i in interactions) / total, 1
        )
        if total
        else 0.0,
        "tool_error_rate": _rate(
            sum(i.tool_failures for i in interactions), sum(i.tool_calls for i in interactions)
        ),
        "verification_failure_rate": _rate(
            sum(1 for i in verified_attempts if i.verification_result != "VERIFIED"),
            len(verified_attempts),
        ),
        "credit_issuance_rate": _rate(sum(1 for i in interactions if i.credits_requested), total),
        "blocked_credit_rate": _rate(
            sum(i.credits_blocked for i in interactions),
            sum(i.credits_requested + i.credits_blocked for i in interactions),
        ),
        "containment_rate": _rate(
            sum(1 for i in interactions if i.outcome not in ESCALATED_OUTCOMES), total
        ),
    }


def from_record(record: dict) -> Interaction:
    """
    Reads one stored conversation into the shape the rates need.

    record: a `conversations` item.

    Returns: an Interaction. Missing fields become their neutral value rather than raising —
             a call that ended before anything was recorded is still a call that happened.
    """
    tools = record.get("tools_invoked") or {}
    verification = record.get("verification") or {}
    escalation = record.get("escalation") or {}

    return Interaction(
        conversation_id=str(record.get("conversation_id", "")),
        outcome=str(record.get("outcome", "ABANDONED")),
        duration_seconds=int(Decimal(str(record.get("duration_seconds", 0)))),
        verification_result=verification.get("result"),
        verification_attempts=int(Decimal(str(verification.get("attempts", 0)))),
        tool_calls=sum(int(Decimal(str(t.get("count", 0)))) for t in tools.values()),
        tool_failures=sum(int(Decimal(str(t.get("failures", 0)))) for t in tools.values()),
        credits_requested=int(Decimal(str(record.get("credits_requested", 0)))),
        credits_blocked=int(Decimal(str(record.get("credits_blocked", 0)))),
        escalation_required=bool(escalation.get("required")),
        repeat_within_window=bool(record.get("repeat_within_window")),
        financial_actions=[str(a) for a in (record.get("financial_actions") or [])],
    )

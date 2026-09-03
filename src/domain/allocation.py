"""What the agent is permitted to do with a matched payment.

Allocating a payment to an invoice is the golden path's one mutation, and it happens on a
language model's recommendation. This rule decides how far that recommendation may go. Under
the shipped policy it goes nowhere: allocation_authority_max is zero, so every allocation is
proposed for a person to confirm (FR-012).

The authority threshold is real rather than decorative. The policy value exists, so the
branch it controls exists and is tested — raising it is then a deliberate act with a known
consequence rather than an undefined one.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

# The only state from which an allocation may be proposed. A pending payment may yet fail,
# and an already-allocated one is settled; proposing either would put money under review
# that is not there to move.
ALLOCATABLE_STATUS = "UNALLOCATED"


class AllocationDecision(StrEnum):
    PROPOSE_REVIEW = "PROPOSE_REVIEW"
    AUTO_ALLOCATE = "AUTO_ALLOCATE"
    ALREADY_UNDER_REVIEW = "ALREADY_UNDER_REVIEW"
    ALREADY_ALLOCATED = "ALREADY_ALLOCATED"
    NOT_ALLOWED = "NOT_ALLOWED"


@dataclass(frozen=True)
class AllocationOutcome:
    """
    What may happen to this payment, and what the audit record should say.

    decision:                what the handler should do.
    previous_status:         the payment's status before the action, for the audit record.
    new_status:              the status it should move to, or None when nothing moves.
    requires_human_approval: whether a person must still confirm the outcome.
    authorizing_rule:        the named rule that permitted this, for the audit record
                             (FR-041).
    """

    decision: AllocationDecision
    previous_status: str
    new_status: str | None
    requires_human_approval: bool
    authorizing_rule: str


def decide_allocation(
    payment_status: str,
    payment_amount: Decimal,
    authority_max: Decimal,
) -> AllocationOutcome:
    """
    Decides whether a matched payment may be allocated, proposed, or neither.

    payment_status: the payment's current ledger status.
    payment_amount: the amount, for comparison against the agent's authority.
    authority_max:  the largest allocation the agent may complete alone, from policy. Zero
                    means none, which is the shipped default.

    Returns: an AllocationOutcome. State is checked before authority, so a generous
             threshold can never become a way to allocate a payment that has not arrived.
    """
    if payment_status == "UNDER_REVIEW":
        return AllocationOutcome(
            decision=AllocationDecision.ALREADY_UNDER_REVIEW,
            previous_status=payment_status,
            new_status=None,
            requires_human_approval=True,
            authorizing_rule="idempotent_repeat",
        )

    if payment_status == "ALLOCATED":
        return AllocationOutcome(
            decision=AllocationDecision.ALREADY_ALLOCATED,
            previous_status=payment_status,
            new_status=None,
            requires_human_approval=False,
            authorizing_rule="idempotent_repeat",
        )

    # Anything else, including a status this code does not recognise, is refused. A ledger
    # that has moved on without this rule is not something to guess about.
    if payment_status != ALLOCATABLE_STATUS:
        return AllocationOutcome(
            decision=AllocationDecision.NOT_ALLOWED,
            previous_status=payment_status,
            new_status=None,
            requires_human_approval=False,
            authorizing_rule="payment_not_allocatable",
        )

    if payment_amount <= authority_max:
        return AllocationOutcome(
            decision=AllocationDecision.AUTO_ALLOCATE,
            previous_status=payment_status,
            new_status="ALLOCATED",
            requires_human_approval=False,
            authorizing_rule="allocation_authority_max",
        )

    return AllocationOutcome(
        decision=AllocationDecision.PROPOSE_REVIEW,
        previous_status=payment_status,
        new_status="UNDER_REVIEW",
        requires_human_approval=True,
        authorizing_rule="allocation_above_authority",
    )

"""Whether a goodwill credit may be granted, and for how much.

The one place the agent gives something away without a person confirming it, which makes it
the rule most worth getting right. Three limits apply, and they exist for different reasons:

- **Per request** bounds what a single call can cost.
- **Rolling twelve months** stops the first being defeated by asking repeatedly.
- **The charge's own amount** stops a customer being credited more than they were billed.

The third is a data-integrity rule rather than a policy one, which is why it is checked
separately and survives any change to the other two.

Pure. Given the request, the charge, the history and the risk, decide.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from src.domain.risk import RiskLevel

# Ledger statuses that mean a charge is already being looked at by a person. Crediting one
# would pre-empt their decision.
DISPUTED_STATUSES = frozenset({"UNDER_REVIEW", "PENDING_APPROVAL"})

# Only an invoice can be credited. A payment and a credit note are self-evidently not
# charges; an adjustment is excluded because it may be signed either way, so "credit this
# adjustment" has no unambiguous meaning. If one genuinely needs crediting, that is a
# person's decision.
CREDITABLE_TYPES = frozenset({"INVOICE"})

# A credit that was asked for and refused was never given, so it does not count against the
# customer's window. One awaiting approval does: it is committed even if unconfirmed, and
# excluding it would let a caller stack requests faster than they can be approved.
COUNTED_CREDIT_STATUSES = frozenset({"APPROVED", "PENDING_APPROVAL"})

# Approximated as 365 days a year. A calendar-exact window would move the boundary by a day
# or two depending on leap years, which changes nothing about the rule and adds a dependency.
DAYS_PER_MONTH = Decimal("30.4375")


class CreditOutcome(StrEnum):
    GRANTED = "GRANTED"
    DENIED_LIMIT = "DENIED_LIMIT"
    DENIED_RISK = "DENIED_RISK"
    DENIED_INELIGIBLE = "DENIED_INELIGIBLE"
    DENIED_EXCEEDS_ENTRY = "DENIED_EXCEEDS_ENTRY"


@dataclass(frozen=True)
class CreditableEntry:
    """
    A charge a credit could be applied to.

    entry_id:         the ledger entry.
    amount:           what the customer was billed, positive.
    already_credited: what has been credited against it, positive.
    has_open_dispute: whether a person is already reviewing it.
    """

    entry_id: str
    amount: Decimal
    already_credited: Decimal
    has_open_dispute: bool

    @property
    def remaining(self) -> Decimal:
        """How much of this charge is still uncredited."""
        return self.amount - self.already_credited


@dataclass(frozen=True)
class CreditDecision:
    """
    The outcome, and everything the audit record needs.

    outcome:             what may happen.
    authorizing_rule:    the named rule that decided it, granted or refused (FR-041).
    credit_amount:       what would be issued, on a grant.
    rolling_total_after: the customer's twelve-month total including this request, so the
                         agent can be accurate if asked.
    """

    outcome: CreditOutcome
    authorizing_rule: str
    credit_amount: Decimal = Decimal("0")
    rolling_total_after: Decimal = Decimal("0")


def evaluate_credit(
    requested_amount: Decimal,
    entry: CreditableEntry | None,
    account_status: str,
    rolling_total: Decimal,
    risk_level: RiskLevel,
    max_per_request: Decimal,
    max_rolling: Decimal,
) -> CreditDecision:
    """
    Decides whether a goodwill credit may be granted.

    requested_amount: what the caller asked for, in CHF.
    entry:            the charge it applies to, or None when the agent named one that does
                      not exist or cannot be credited.
    account_status:   from the identity record. Only an active account may be credited.
    rolling_total:    credits already given in the window, excluding this request.
    risk_level:       the aggregate of the caller's risk signals.
    max_per_request:  inclusive ceiling on one credit, from policy.
    max_rolling:      inclusive ceiling on the window total, from policy.

    Returns: a CreditDecision.

    Checks run in a deliberate order, so the reported reason is the most specific true one:
    eligibility first, because a request that makes no sense should not be attributed to the
    caller's risk profile; then risk, which overrides the money rules entirely (FR-015);
    then the charge's own amount; then the two ceilings. A caller with a suspended account
    asking for CHF 900 is told the account is the problem, not the amount.
    """
    ineligible = _ineligibility(requested_amount, entry, account_status)
    if ineligible:
        return CreditDecision(CreditOutcome.DENIED_INELIGIBLE, ineligible)

    # The point of a risk check is that it can refuse something the rules would allow.
    if risk_level is RiskLevel.HIGH:
        return CreditDecision(CreditOutcome.DENIED_RISK, "risk_override")

    assert entry is not None  # narrowed by _ineligibility

    if requested_amount > entry.remaining:
        return CreditDecision(CreditOutcome.DENIED_EXCEEDS_ENTRY, "entry_amount_cap")

    if requested_amount > max_per_request:
        return CreditDecision(CreditOutcome.DENIED_LIMIT, "credit_max_per_request")

    total_after = rolling_total + requested_amount
    if total_after > max_rolling:
        return CreditDecision(
            CreditOutcome.DENIED_LIMIT, "credit_max_rolling", rolling_total_after=total_after
        )

    return CreditDecision(
        outcome=CreditOutcome.GRANTED,
        authorizing_rule="within_credit_limits",
        credit_amount=requested_amount,
        rolling_total_after=total_after,
    )


def _ineligibility(
    requested_amount: Decimal, entry: CreditableEntry | None, account_status: str
) -> str | None:
    """
    Checks whether the request makes sense at all.

    Returns: the name of the failing rule, or None when the request is coherent.
    """
    if requested_amount <= 0:
        # A negative credit is a charge, and the agent has no authority to bill anyone.
        return "invalid_amount"
    if account_status != "ACTIVE":
        return "account_status"
    if entry is None:
        # A credit with no identified charge cannot be reconciled by anyone afterwards
        # (FR-013a).
        return "no_such_entry"
    if entry.has_open_dispute:
        return "open_dispute"
    return None


def rolling_credit_total(entries: list[dict], today: date, window_months: int) -> Decimal:
    """
    Sums the goodwill a customer has already received inside the window.

    entries:       every ledger entry for the customer.
    today:         the date the window is measured back from. Passed in rather than read
                   from the clock, so the rule stays pure and testable at its boundaries.
    window_months: how far back to look, from policy.

    Returns: the total as a positive amount. Credits are stored negative because they reduce
             what is owed; a limit expressed as "CHF 500" is easier to reason about than
             "-500".
    """
    cutoff = today - timedelta(days=int(Decimal(window_months) * DAYS_PER_MONTH))

    return sum(
        (
            abs(Decimal(str(entry["amount"])))
            for entry in entries
            if entry.get("type") == "CREDIT_NOTE"
            and entry.get("status") in COUNTED_CREDIT_STATUSES
            and date.fromisoformat(str(entry["entry_date"])) >= cutoff
        ),
        Decimal("0"),
    )


def entry_from_ledger(entry: dict | None, existing_credits: list[dict]) -> CreditableEntry | None:
    """
    Turns a ledger row into the charge the rule reasons about.

    entry:            the ledger entry the agent named, or None when it does not exist.
    existing_credits: the customer's credit notes, for totalling what is already applied to
                      this charge.

    Returns: a CreditableEntry, or None when the entry is missing or is not something a
             credit can attach to. Returning None rather than raising because the agent
             naming a charge that does not exist is a refusal, not a fault.
    """
    if not entry or entry.get("type") not in CREDITABLE_TYPES:
        return None

    entry_id = str(entry["entry_id"])
    already = sum(
        (
            abs(Decimal(str(credit["amount"])))
            for credit in existing_credits
            if entry_id in (credit.get("allocated_to") or [])
            and credit.get("status") in COUNTED_CREDIT_STATUSES
        ),
        Decimal("0"),
    )

    return CreditableEntry(
        entry_id=entry_id,
        amount=abs(Decimal(str(entry["amount"]))),
        already_credited=already,
        has_open_dispute=entry.get("status") in DISPUTED_STATUSES,
    )

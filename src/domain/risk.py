"""Risk signals, and what an accumulation of them means.

A signal is a fact worth remembering about how a caller behaved, not a judgement about who
they are. Most are individually unremarkable — people do mistype their email, and companies
do call twice in a week. What matters is the pattern, and the pattern is evaluated here
rather than by the model, because a model asked "does this seem suspicious?" will sometimes
say yes about an ordinary customer and no about a determined one.

Pure: given signals, decide. Recording them is an effect and lives in conversation_state.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class SignalType(StrEnum):
    """The signals FR-016 requires, plus the two that verification produces.

    Named for what was observed, never for what it might mean. "SUSPECTED_GUESSING" describes
    a caller offering many values for one field; it does not assert fraud, and nothing in the
    system may tell a caller it does.
    """

    REPEATED_FAILED_VERIFICATION = "REPEATED_FAILED_VERIFICATION"
    SUSPECTED_GUESSING = "SUSPECTED_GUESSING"
    SUSPECTED_THRESHOLD_SPLITTING = "SUSPECTED_THRESHOLD_SPLITTING"
    CONFLICTING_IDENTITY_DATA = "CONFLICTING_IDENTITY_DATA"
    HIGH_CONTACT_FREQUENCY = "HIGH_CONTACT_FREQUENCY"
    REPEATED_DISPUTES = "REPEATED_DISPUTES"
    UNUSUAL_PAYMENT_BEHAVIOUR = "UNUSUAL_PAYMENT_BEHAVIOUR"


# Signals that, alone, are enough to stop an automated decision. The rest accumulate: one
# repeated dispute is a customer with a problem, several is a pattern worth a human's
# attention.
DECISIVE_SIGNALS = frozenset(
    {
        SignalType.SUSPECTED_GUESSING,
        SignalType.REPEATED_FAILED_VERIFICATION,
        SignalType.CONFLICTING_IDENTITY_DATA,
        SignalType.SUSPECTED_THRESHOLD_SPLITTING,
    }
)

# How many accumulating signals amount to a decisive one.
ACCUMULATION_THRESHOLD = 2


class RiskLevel(StrEnum):
    NONE = "NONE"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"


@dataclass(frozen=True)
class RiskSignal:
    """
    One observation about a caller's behaviour.

    signal_type:     what was observed.
    evidence:        why it was raised, in terms a human reviewer can check. Never a value
                     the caller supplied and never a stored value — a count, a field name, a
                     comparison outcome.
    conversation_id: the call it was observed on.
    customer_id:     the account it applies to, where one was established. A caller who never
                     named an account still produces signals; they belong to the conversation
                     rather than to anyone.
    observed_at:     when.
    """

    signal_type: SignalType
    evidence: str
    conversation_id: str
    customer_id: str | None = None
    observed_at: str = ""

    def __post_init__(self) -> None:
        if not self.observed_at:
            object.__setattr__(self, "observed_at", datetime.now(UTC).isoformat())


def evaluate(signals: list[RiskSignal]) -> RiskLevel:
    """
    Decides what a set of signals amounts to.

    signals: everything observed about this caller, across this call and recent ones.

    Returns: HIGH when any decisive signal is present or enough accumulating ones are,
             ELEVATED when there is something but not enough, NONE otherwise.

    HIGH overrides a passing eligibility result everywhere it is consulted (FR-015): the
    point of a risk check is that it can say no to something the rules would otherwise allow.
    """
    if not signals:
        return RiskLevel.NONE

    if any(signal.signal_type in DECISIVE_SIGNALS for signal in signals):
        return RiskLevel.HIGH

    if len(signals) >= ACCUMULATION_THRESHOLD:
        return RiskLevel.HIGH

    return RiskLevel.ELEVATED


# --- Detecting patterns in a customer's history ---------------------------------------
#
# Every threshold below is a judgement about where ordinary behaviour ends, and each is set
# so that the fixtures' honest customers stay clear of it. They are constants rather than
# policy parameters because they describe what a pattern looks like rather than what the
# company permits — if they need tuning per deployment, that is a sign the detection is
# wrong rather than the number.

# How far back a pattern is looked for. Matches the credit window, so a customer who has
# aged out of the credit ceiling has also aged out of the suspicion.
HISTORY_WINDOW_DAYS = 365

# A credit at or above this fraction of the per-request ceiling counts as "near the limit".
# Someone taking CHF 5 repeatedly is not working around a CHF 100 ceiling; someone taking
# CHF 85 to CHF 100 repeatedly might be.
NEAR_LIMIT_FRACTION = Decimal("0.8")

# Three near-limit credits inside the window. Two is a coincidence — a customer can have two
# bad deliveries in a year — and flagging two would put a note on ordinary accounts.
THRESHOLD_SPLITTING_COUNT = 3

# Calls in the last 30 days that suggest something is going wrong rather than someone being
# unlucky.
CONTACT_FREQUENCY_DAYS = 30
CONTACT_FREQUENCY_COUNT = 5

# Escalations inside the window. One is a customer who had a problem and was helped.
REPEATED_DISPUTES_COUNT = 3

# Payments that arrived and never matched an invoice. One is the golden path — the entire
# demo is a customer calling about exactly this — so the threshold must sit above it or
# every disputed-invoice caller starts the conversation flagged.
UNALLOCATED_PAYMENT_COUNT = 3


def detect_history_signals(
    ledger: list[dict],
    conversations: list[dict],
    customer_id: str,
    conversation_id: str,
    today: date,
    max_per_request: Decimal,
) -> list[RiskSignal]:
    """
    Looks for patterns across a customer's recent history.

    ledger:          every ledger entry for the customer.
    conversations:   their recent conversation records, each with started_at and outcome.
    customer_id:     the account the signals belong to.
    conversation_id: the call they were observed on.
    today:           the date windows are measured back from. Passed in rather than read
                     from the clock, so the rule stays pure and testable at its boundaries.
    max_per_request: the credit ceiling, for deciding what counts as "near the limit".

    Returns: one signal per pattern found, empty when the history is unremarkable.

    Each signal is a statement about shape, not about the person. Whether the caller is then
    refused is decided elsewhere: "this looks unusual" and "this is not allowed" are
    different statements, and conflating them is how an honest customer ends up accused.
    """
    cutoff = today - timedelta(days=HISTORY_WINDOW_DAYS)
    signals: list[RiskSignal] = []

    def signal(signal_type: SignalType, evidence: str) -> None:
        signals.append(
            RiskSignal(
                signal_type=signal_type,
                evidence=evidence,
                conversation_id=conversation_id,
                customer_id=customer_id,
            )
        )

    near_limit = [
        entry
        for entry in ledger
        if entry.get("type") == "CREDIT_NOTE"
        and _within(entry.get("entry_date"), cutoff)
        and abs(Decimal(str(entry["amount"]))) >= max_per_request * NEAR_LIMIT_FRACTION
    ]
    if len(near_limit) >= THRESHOLD_SPLITTING_COUNT:
        # Counts and comparisons only. Naming the amounts would put figures in front of a
        # reviewer that the caller could later be confronted with (FR-016).
        signal(
            SignalType.SUSPECTED_THRESHOLD_SPLITTING,
            f"{len(near_limit)} credits at or near the per-request limit in the last "
            f"{HISTORY_WINDOW_DAYS} days",
        )

    recent_contact_cutoff = today - timedelta(days=CONTACT_FREQUENCY_DAYS)
    recent_calls = [c for c in conversations if _within(c.get("started_at"), recent_contact_cutoff)]
    if len(recent_calls) >= CONTACT_FREQUENCY_COUNT:
        signal(
            SignalType.HIGH_CONTACT_FREQUENCY,
            f"{len(recent_calls)} calls in the last {CONTACT_FREQUENCY_DAYS} days",
        )

    escalated = [
        c
        for c in conversations
        if _within(c.get("started_at"), cutoff) and c.get("outcome") == "ESCALATED"
    ]
    if len(escalated) >= REPEATED_DISPUTES_COUNT:
        signal(
            SignalType.REPEATED_DISPUTES,
            f"{len(escalated)} calls escalated in the last {HISTORY_WINDOW_DAYS} days",
        )

    unallocated = [
        entry
        for entry in ledger
        if entry.get("type") == "PAYMENT"
        and entry.get("status") == "UNALLOCATED"
        and _within(entry.get("entry_date"), cutoff)
    ]
    if len(unallocated) >= UNALLOCATED_PAYMENT_COUNT:
        signal(
            SignalType.UNUSUAL_PAYMENT_BEHAVIOUR,
            f"{len(unallocated)} payments received that never matched an invoice",
        )

    return signals


def _within(value: object, cutoff: date) -> bool:
    """Whether an ISO date string falls on or after the cutoff. A missing or unreadable date
    is treated as outside the window rather than raising: a malformed row should not stop a
    risk check running."""
    if not value:
        return False
    try:
        return date.fromisoformat(str(value)[:10]) >= cutoff
    except ValueError:
        return False

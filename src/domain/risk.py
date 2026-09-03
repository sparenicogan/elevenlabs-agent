"""Risk signals, and what an accumulation of them means.

A signal is a fact worth remembering about how a caller behaved, not a judgement about who
they are. Most are individually unremarkable — people do mistype their email, and companies
do call twice in a week. What matters is the pattern, and the pattern is evaluated here
rather than by the model, because a model asked "does this seem suspicious?" will sometimes
say yes about an ordinary customer and no about a determined one.

Pure: given signals, decide. Recording them is an effect and lives in conversation_state.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class SignalType(StrEnum):
    """The signals FR-016 requires, plus the two that verification produces.

    Named for what was observed, never for what it might mean. "SUSPECTED_GUESSING" describes
    a caller offering many values for one field; it does not assert fraud, and nothing in the
    system may tell a caller it does.
    """

    REPEATED_FAILED_VERIFICATION = "REPEATED_FAILED_VERIFICATION"
    SUSPECTED_GUESSING = "SUSPECTED_GUESSING"
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

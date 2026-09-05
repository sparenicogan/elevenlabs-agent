"""Counting what a caller has offered, and deciding when they have stopped remembering.

One correction is human. A third distinct value for the same field is somebody working
through possibilities, and the difference matters more than any single wrong answer does
(FR-006a).

Shared by both endpoints that take an answer. It was written twice, once in each, and the two
copies had begun to differ in what they recorded — which is how the rule ends up holding on
one path and not the other.
"""

from src.adapters import secrets
from src.common import conversation_state
from src.common import logging as log
from src.domain.risk import RiskSignal, SignalType
from src.domain.verification import Factor, fingerprint, is_enumerating


def record_and_check(
    conversation_id: str,
    supplied: dict[Factor, str],
    settings,
    contact_id: str | None = None,
) -> tuple[bool, bool]:
    """
    Records the values offered and decides whether the caller is guessing.

    conversation_id: the call.
    supplied:        this attempt's answers, by factor.
    settings:        policy, for the allowance.
    contact_id:      the person, where one was resolved. A caller who matches nobody still
                     produces signals; they belong to the conversation.

    Returns: (whether a field exceeded the allowance, whether this call offered any value not
             already seen). The second is what stops a resent wrong answer counting as a fresh
             failure.

    Values are fingerprinted, never stored: the point of counting attempts is to know how many
    there were, not what they were.
    """
    if not supplied:
        return False, False

    salt = secrets.get("verification/attempt-salt")
    counts, offered_new = conversation_state.record_factor_attempts(
        conversation_id,
        {factor.value: fingerprint(factor, value, salt) for factor, value in supplied.items()},
    )

    offending = is_enumerating(
        {Factor(field): count for field, count in counts.items()},
        settings.guessing_max_distinct_values,
    )
    if not offending:
        return False, offered_new

    conversation_state.record_risk_signal(
        RiskSignal(
            signal_type=SignalType.SUSPECTED_GUESSING,
            # A count and a field name. Never what was offered — recording the guesses would
            # defeat the point of fingerprinting them.
            evidence=(
                f"{counts[offending.value]} distinct values offered for {offending.value}, "
                f"allowance is {settings.guessing_max_distinct_values}"
            ),
            conversation_id=conversation_id,
            customer_id=contact_id,
        )
    )
    log.info(
        "suspected guessing",
        conversation_id=conversation_id,
        customer_id=contact_id or "",
        status="LOCKED",
        attempt=counts[offending.value],
    )
    return True, offered_new

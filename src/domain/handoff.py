"""Composing the summary a person receives when a call is handed over.

Built in the backend rather than left to the model, because FR-019 fixes what it must carry
and a model summarising under time pressure will drop the field that turns out to matter. It
is also the difference between a caller explaining their problem once and explaining it
twice, which is most of what makes a transfer feel competent.

Pure. Given facts, produce text.
"""

# Fields that must never appear in a handoff for an unverified caller, because nothing has
# been established as theirs (FR-019c).
UNVERIFIED_FORBIDDEN_FIELDS = ("invoice", "payment", "balance", "credit", "outstanding")


def compose_handoff(
    reason: str,
    verified: bool,
    conversation_id: str,
    company_name: str | None = None,
    caller_stated_problem: str | None = None,
    caller_self_description: str | None = None,
    notes: str | None = None,
    discrepancy: dict | None = None,
    risk_signals: list[str] | None = None,
) -> str:
    """
    Writes the handoff a person reads before taking the call.

    reason:                  why it escalated.
    verified:                whether identity was established. Changes what may be included
                             at all, not merely how it is phrased.
    conversation_id:         so the recording can be found.
    company_name:            the customer, when verified.
    caller_stated_problem:   what they said they were calling about, in their words.
    caller_self_description: what an unverified caller claimed about themselves.
    notes:                   anything else the agent captured.
    discrepancy:             a field mismatch and the caller's explanation of it.
    risk_signals:            signal types raised on the call, by name.

    Returns: plain text, written to be read aloud or skimmed in a few seconds.
    """
    lines = [f"Escalation: {reason.replace('_', ' ').lower()}."]

    if verified:
        lines.append(f"Caller verified as the contact for {company_name or 'the account'}.")
    else:
        # Stated first and stated plainly. A reader skimming must not assume identity was
        # established, because everything below is unconfirmed.
        lines.append("CALLER NOT VERIFIED. Nothing below is confirmed.")
        if caller_self_description:
            lines.append(f"They said of themselves: {caller_self_description}")

    if caller_stated_problem:
        lines.append(f"They are calling about: {caller_stated_problem}")

    if notes:
        lines.append(f"Notes: {notes}")

    if discrepancy:
        field = discrepancy.get("field", "a field")
        explanation = discrepancy.get("caller_explanation", "UNKNOWN")
        lines.append(f"Discrepancy on {field}; the caller said: {explanation}.")

    if risk_signals:
        lines.append(f"Risk signals raised: {', '.join(sorted(set(risk_signals)))}.")

    lines.append(f"Conversation {conversation_id}.")
    lines.append("The caller has already explained this once. They should not have to again.")

    return "\n".join(lines)

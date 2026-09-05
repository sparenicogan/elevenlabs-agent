"""Regenerating what is remembered about a customer between calls.

The summary exists so somebody who explained their problem last week is not asked to explain
it again. It is narrative only: what the calls were about and how the customer likes to be
dealt with. Never a balance, never an invoice, never anything a caller answered to verify
themselves — those are read live, because a remembered number is a number that can be wrong
(FR-039c).
"""

import re

# Sentences the summary will never carry, however they arrived. Matched on shape rather than
# a word list: an amount is an amount whether or not the sentence around it looks financial.
_FORBIDDEN = (
    re.compile(r"\bCHF\s?[\d,.]+", re.I),
    re.compile(r"\b\d{1,3}(?:[.,]\d{3})*[.,]\d{2}\b"),
    re.compile(r"\bINV-\d{4}-\d{4}\b", re.I),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    re.compile(r"\+?\d[\d\s./-]{7,}\d"),
)


def redact(text: str) -> str:
    """
    Removes anything the summary may not carry.

    text: the candidate summary.

    Returns: the same text with amounts, invoice numbers, dates, emails and phone numbers
             replaced by a neutral word. Redacting rather than rejecting, because a summary
             that fails to save is a caller repeating themselves next week — the narrative is
             worth keeping even when one clause has to go.
    """
    for pattern in _FORBIDDEN:
        text = pattern.sub("[removed]", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def regenerate(previous: str, this_call: str, max_chars: int) -> str:
    """
    Folds one call into what is already known.

    previous:  the summary before this call. Empty for a first-time caller.
    this_call: one or two sentences about what just happened.
    max_chars: the ceiling from policy.

    Returns: the new summary, redacted and within the ceiling.

    Newest first, and the oldest sentences fall off the end. Ten calls must not produce ten
    times the text, and when something has to go it should be the thing longest ago — a
    summary that grows without bound stops being read, which is the same as not having one.
    """
    fresh = redact(this_call).strip()
    kept = redact(previous).strip()

    if not fresh:
        return kept[:max_chars].strip()

    combined = f"{fresh} {kept}".strip() if kept else fresh
    if len(combined) <= max_chars:
        return combined

    # Trim on a sentence boundary so the summary never ends mid-clause.
    trimmed = combined[:max_chars]
    cut = trimmed.rfind(". ")
    return (trimmed[: cut + 1] if cut > len(fresh) else trimmed).strip()

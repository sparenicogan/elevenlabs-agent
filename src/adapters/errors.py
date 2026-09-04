"""Maps external failures to the structured error envelope from contracts/tools.md.

Principle III turns on one distinction: a dependency being unreachable is not the same
answer as an authoritative "no". Everything external fails through here so the agent
always receives a status it can speak safely rather than a stack trace it might guess at.
"""

from dataclasses import dataclass
from enum import StrEnum


class ErrorCategory(StrEnum):
    TIMEOUT = "TIMEOUT"
    DEPENDENCY_DOWN = "DEPENDENCY_DOWN"
    VALIDATION = "VALIDATION"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    INTERNAL = "INTERNAL"


# What the agent should say, not merely what went wrong.
#
# Directive because a neutral description leaves the model a gap to fill, and conversation
# testing showed it sometimes fills one by claiming the action succeeded: an allocation was
# refused and the caller was told a colleague would confirm it within twenty-four hours.
# Handing over a sentence is more reliable than a prompt rule about what not to say — the
# rule has to be recalled, the sentence is already in the response.
#
# None asserts a financial fact: at the point these are returned the backend does not know
# one (FR-011). Each states that nothing changed, because that is the part most often
# narrated wrongly.
_MESSAGE_HINTS = {
    ErrorCategory.TIMEOUT: (
        "This took longer than expected and did not go through. Tell the caller you could "
        "not complete it, and offer to have a colleague pick it up."
    ),
    ErrorCategory.DEPENDENCY_DOWN: (
        "That information is temporarily unavailable and nothing was changed. Tell the "
        "caller you cannot access it right now, never what it would have said, and offer to "
        "have a colleague follow up."
    ),
    ErrorCategory.VALIDATION: (
        "Some of the details could not be read and nothing was changed. Ask the caller to "
        "repeat the detail rather than guessing at it."
    ),
    ErrorCategory.NOT_AUTHORIZED: (
        "This is not available at this point in the call and nothing was changed. Do not "
        "describe it as done."
    ),
    ErrorCategory.INTERNAL: (
        "Something went wrong on our side and the action did not complete. Tell the caller "
        "plainly, and arrange for a colleague to take it on."
    ),
}

# A read may be retried once; a mutation may not (FR-023). Categories that indicate the
# request itself was wrong are never retryable, however the caller asks.
_RETRYABLE = {ErrorCategory.TIMEOUT, ErrorCategory.DEPENDENCY_DOWN}


@dataclass(frozen=True)
class ToolError(Exception):
    """
    A failure the agent must not present as an answer.

    category: which kind of failure, from ErrorCategory.
    detail:   internal context for the logs. Never returned to the agent, because it can
              contain identifiers and store internals.
    """

    category: ErrorCategory
    detail: str = ""

    def to_response(self) -> dict:
        """
        Renders the error envelope the tool endpoint returns.

        Returns: dict with status SERVICE_UNAVAILABLE, the error category, whether a retry
                 is permitted, and a caller-safe phrase. Never carries `detail`.
        """
        return {
            "status": "SERVICE_UNAVAILABLE",
            "error_category": str(self.category),
            "retryable": self.category in _RETRYABLE,
            "message_hint": _MESSAGE_HINTS[self.category],
        }

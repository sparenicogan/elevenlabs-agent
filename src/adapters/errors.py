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


# Phrasing the agent may paraphrase. None of these asserts a financial fact, because at
# the point they are returned the backend does not know one (FR-011).
_MESSAGE_HINTS = {
    ErrorCategory.TIMEOUT: "The system is taking longer than expected to respond.",
    ErrorCategory.DEPENDENCY_DOWN: "That information is temporarily unavailable.",
    ErrorCategory.VALIDATION: "Some of the details provided could not be read.",
    ErrorCategory.NOT_AUTHORIZED: "That action is not available at this point in the call.",
    ErrorCategory.INTERNAL: "Something went wrong on our side.",
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

"""Structured logging with a field allowlist.

The allowlist is the point. FR-029 forbids verification answers and raw identity fields in
logs, and a rule that relies on every future caller remembering it will eventually be
broken. Anything not named here is dropped before it reaches CloudWatch.
"""

import json
import logging
import os
import sys
from typing import Any

# Fields that may appear in a log line. Identifiers and enumerated statuses only: nothing
# a person could be identified by, and nothing a caller said.
ALLOWED_FIELDS = frozenset(
    {
        "conversation_id",
        "customer_id",
        "entry_id",
        "ticket_id",
        "agent_version",
        "tool",
        "status",
        "error_category",
        # Which detail a check was about, e.g. "email". The name, never the value.
        "field",
        # Exception text from an adapter. Never caller-supplied input, never a stored value.
        "error_detail",
        "action",
        "latency_ms",
        "attempt",
        "retryable",
        "language",
        "rule_applied",
        "outcome",
        "event_type",
        # Per-run counts from the applier. Numbers only, never anything a caller said.
        "applied",
        "already_applied",
        "refused",
        "failed",
        "message",
    }
)

_logger = logging.getLogger("voice-agent")


def configure() -> None:
    """Sets up JSON logging on stdout. Called once per Lambda cold start."""
    if _logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    _logger.propagate = False


def log(level: str, message: str, **fields: Any) -> None:
    """
    Emits one JSON log line, dropping any field not on the allowlist.

    level:   standard logging level name, e.g. "INFO" or "ERROR".
    message: short human-readable summary. Must not interpolate caller-supplied values.
    fields:  structured context. Keys outside ALLOWED_FIELDS are silently discarded, so a
             mistaken call site leaks nothing.
    """
    configure()
    safe = {k: v for k, v in fields.items() if k in ALLOWED_FIELDS}
    safe["message"] = message
    _logger.log(logging.getLevelName(level), json.dumps(safe, default=str))


def info(message: str, **fields: Any) -> None:
    log("INFO", message, **fields)


def error(message: str, **fields: Any) -> None:
    log("ERROR", message, **fields)

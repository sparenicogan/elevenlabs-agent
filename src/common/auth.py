"""API key authentication for tool endpoints and HMAC validation for the post-call webhook."""

import hashlib
import hmac
import time

from src.adapters.errors import ErrorCategory, ToolError

# Rejects a replayed webhook whose signature is still cryptographically valid but stale.
SIGNATURE_MAX_AGE_SECONDS = 1800


def require_api_key(headers: dict[str, str], expected: str) -> None:
    """
    Rejects a tool call that does not carry the shared API key.

    headers:  request headers, case-insensitive by convention from API Gateway.
    expected: the key read from Secrets Manager.

    Returns: nothing on success. Raises ToolError(NOT_AUTHORIZED) otherwise.
    """
    supplied = headers.get("x-api-key") or headers.get("X-Api-Key") or ""

    # Constant-time comparison: a timing difference here would leak the key one byte
    # at a time to anyone who can call the endpoint.
    if not hmac.compare_digest(supplied, expected):
        raise ToolError(ErrorCategory.NOT_AUTHORIZED, "missing or incorrect api key")


def verify_webhook_signature(header: str, body: str, secret: str, now: float | None = None) -> None:
    """
    Validates the ElevenLabs post-call webhook signature.

    header: the ElevenLabs-Signature value, formatted "t=<unix>,v0=<hex digest>".
    body:   the raw request body, exactly as received. Re-serialising it changes the digest.
    secret: the shared HMAC secret from Secrets Manager.
    now:    current unix time; injectable so the staleness check is testable.

    Returns: nothing on success. Raises ToolError(NOT_AUTHORIZED) on a bad or stale signature.
    """
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    timestamp, digest = parts.get("t"), parts.get("v0")

    if not timestamp or not digest:
        raise ToolError(ErrorCategory.NOT_AUTHORIZED, "malformed signature header")

    age = (now if now is not None else time.time()) - int(timestamp)
    if abs(age) > SIGNATURE_MAX_AGE_SECONDS:
        raise ToolError(ErrorCategory.NOT_AUTHORIZED, "signature timestamp outside window")

    expected = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, digest):
        raise ToolError(ErrorCategory.NOT_AUTHORIZED, "signature mismatch")

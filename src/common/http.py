"""The response shape every tool endpoint returns, and the envelope it returns on failure.

Seven endpoints carried byte-identical copies of both. They live here so the wire format is
one edit rather than seven, and so an endpoint added later cannot quietly invent a different
shape for the agent to parse.
"""

import json

from src.adapters.errors import ToolError
from src.common import logging as log


def respond(code: int, body: dict) -> dict:
    """
    Renders the proxy response API Gateway expects.

    code: the HTTP status. Tool endpoints answer 200 even for a handled failure, because the
          agent needs a body it can speak from rather than a transport error it might guess at.
    body: the JSON-serialisable payload.

    Returns: dict with statusCode, JSON content-type header, and the serialised body.
    """
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def failed(endpoint: str, error: ToolError) -> dict:
    """
    Logs a handled failure and renders the envelope the agent receives in place of an answer.

    endpoint: the tool's name, for the log line. Passed rather than derived so the name in
              CloudWatch survives the function being renamed or wrapped.
    error:    the failure. Its detail reaches the log and never the caller, because it can
              carry identifiers and store internals.

    Returns: a 200 proxy response carrying the error envelope from contracts/tools.md.
    """
    log.error(
        f"{endpoint} failed",
        error_category=str(error.category),
        error_detail=error.detail,
    )
    return respond(200, error.to_response())

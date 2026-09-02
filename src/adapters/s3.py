"""Transcript storage. Expiry is the bucket's job, not this module's (FR-038a)."""

import json
import os
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

from src.adapters.errors import ErrorCategory, ToolError

_BUCKET = os.environ.get("TRANSCRIPTS_BUCKET", "")
_client = None


def _s3():
    global _client
    if _client is None:
        _client = boto3.client("s3")
    return _client


def transcript_key(customer_id: str, conversation_id: str, when: datetime) -> str:
    """
    Builds the object key for one conversation's transcript.

    customer_id:     the verified customer. Partitioning by customer is what makes a
                     per-customer deletion request answerable with a prefix delete.
    conversation_id: the call.
    when:            the call's start time, which sets the year and month path segments.

    Returns: the key, per data-model.md.
    """
    return (
        f"customers/{customer_id}/conversations/"
        f"{when.year:04d}/{when.month:02d}/{conversation_id}.json"
    )


def put_transcript(key: str, payload: dict) -> None:
    """
    Writes one transcript.

    key:     from transcript_key.
    payload: the raw post-call payload, stored whole so the record is not shaped by what
             this version of the code thought was interesting.

    Returns: nothing. Raises ToolError(DEPENDENCY_DOWN) on failure, which post-call
             processing records without discarding the steps that already succeeded.
    """
    try:
        _s3().put_object(
            Bucket=_BUCKET,
            Key=key,
            Body=json.dumps(payload).encode(),
            ContentType="application/json",
        )
    except ClientError as exc:
        raise ToolError(ErrorCategory.DEPENDENCY_DOWN, f"put transcript: {exc}") from exc

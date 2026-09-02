"""Secret reads, cached for the life of the execution environment."""

import os

import boto3
from botocore.exceptions import ClientError

from src.adapters.errors import ErrorCategory, ToolError

_PROJECT = os.environ.get("PROJECT", "voice-agent")
_cache: dict[str, str] = {}
_client = None


def get(name: str) -> str:
    """
    Reads one secret value.

    name: the path under the project prefix, e.g. "tools/api-key".

    Returns: the secret string. Cached per execution environment. Raises
             ToolError(DEPENDENCY_DOWN) when unreachable.
    """
    global _client
    if name in _cache:
        return _cache[name]

    if _client is None:
        _client = boto3.client("secretsmanager")

    try:
        value = _client.get_secret_value(SecretId=f"{_PROJECT}/{name}")["SecretString"]
    except ClientError as exc:
        raise ToolError(ErrorCategory.DEPENDENCY_DOWN, f"secret read: {exc}") from exc

    _cache[name] = value
    return value

"""Policy parameter reads, cached for the life of the execution environment.

Caching at cold start keeps the parameter read off the hot path. A policy change takes
effect within one Lambda lifecycle, which is the intended tradeoff (research D9).
"""

import os

import boto3
from botocore.exceptions import ClientError

from src.adapters.errors import ErrorCategory, ToolError

_PROJECT = os.environ.get("PROJECT", "voice-agent")
_cache: dict[str, str] = {}
_client = None


def get_all_policy() -> dict[str, str]:
    """
    Reads every policy parameter under the project's policy path.

    Returns: parameter name (without path prefix) to raw string value. Cached after the
             first call. Raises ToolError(DEPENDENCY_DOWN) if the store is unreachable and
             nothing is cached — the handler must fail rather than invent a threshold.
    """
    global _client
    if _cache:
        return _cache

    if _client is None:
        _client = boto3.client("ssm")

    try:
        paginator = _client.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=f"/{_PROJECT}/policy", Recursive=True):
            for param in page["Parameters"]:
                _cache[param["Name"].rsplit("/", 1)[-1]] = param["Value"]
    except ClientError as exc:
        raise ToolError(ErrorCategory.DEPENDENCY_DOWN, f"policy read: {exc}") from exc

    return _cache

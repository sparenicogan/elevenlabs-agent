"""Conditional-write idempotency for every mutating operation.

FR-022. Voice calls drop and callers repeat themselves; without this a retry becomes a
second credit. A duplicate returns the original result rather than an error, so a repeat
is indistinguishable from a first call to the person on the phone.
"""

import hashlib


def key(*parts: str) -> str:
    """
    Builds a deterministic idempotency key from the natural identifiers of an action.

    parts: the identifiers that make the action unique, e.g. conversation, entry, action
           name. Order matters and must be stable across retries.

    Returns: a hex digest. Hashed rather than concatenated so the key length is fixed
             regardless of how many identifiers an action needs.
    """
    return hashlib.sha256("|".join(parts).encode()).hexdigest()

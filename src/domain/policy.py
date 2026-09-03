"""Typed policy configuration.

Principle VIII: every threshold is a parameter, and no handler contains a literal. Typing
them here means a malformed parameter fails loudly at load rather than silently comparing
a string to a number somewhere in a credit decision.
"""

from dataclasses import dataclass
from decimal import Decimal

from src.adapters import ssm


@dataclass(frozen=True)
class Policy:
    """
    The tunable rules, as read from the parameter store.

    required_factor_count:     independent factors needed to verify (FR-003).
    verification_max_attempts: failures before the account locks (FR-006).
    payment_date_tolerance_days: how many calendar days earlier than the record a caller's
                               transfer date may be. Backward only (FR-010b).
    reference_typo_max_distance: edit distance within which a payment reference counts as a
                               mistyped invoice id, when it resolves to no invoice (FR-010f).
    credit_max_per_request:    inclusive ceiling on a single credit, in CHF (FR-013b).
    credit_max_rolling:        inclusive ceiling on the rolling-window total, in CHF.
    credit_window_months:      length of the rolling window (FR-014).
    allocation_authority_max:  largest allocation the agent may complete alone, in CHF.
                               Zero means every allocation goes to a human (FR-012).
    tool_timeout_seconds:      the agent's budget for one tool call (FR-023).
    read_retry_count:          retries permitted on idempotent reads. Mutations get none.
    summary_max_chars:         cap on the per-customer summary (FR-039a).
    resolution_target_hours:   what the agent promises the caller.
    """

    required_factor_count: int
    verification_max_attempts: int
    payment_date_tolerance_days: int
    reference_typo_max_distance: int
    credit_max_per_request: Decimal
    credit_max_rolling: Decimal
    credit_window_months: int
    allocation_authority_max: Decimal
    tool_timeout_seconds: int
    read_retry_count: int
    summary_max_chars: int
    resolution_target_hours: int


def load() -> Policy:
    """
    Reads and types the policy parameters.

    Returns: a Policy. Raises ToolError(DEPENDENCY_DOWN) via the adapter when the store is
             unreachable, because a handler that cannot read its thresholds must refuse
             rather than assume one.
    """
    raw = ssm.get_all_policy()
    return Policy(
        required_factor_count=int(raw["required_factor_count"]),
        verification_max_attempts=int(raw["verification_max_attempts"]),
        payment_date_tolerance_days=int(raw["payment_date_tolerance_days"]),
        reference_typo_max_distance=int(raw["reference_typo_max_distance"]),
        credit_max_per_request=Decimal(raw["credit_max_per_request"]),
        credit_max_rolling=Decimal(raw["credit_max_rolling"]),
        credit_window_months=int(raw["credit_window_months"]),
        allocation_authority_max=Decimal(raw["allocation_authority_max"]),
        tool_timeout_seconds=int(raw["tool_timeout_seconds"]),
        read_retry_count=int(raw["read_retry_count"]),
        summary_max_chars=int(raw["summary_max_chars"]),
        resolution_target_hours=int(raw["resolution_target_hours"]),
    )

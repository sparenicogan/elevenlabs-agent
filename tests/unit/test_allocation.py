"""Allocation legality (FR-012, FR-022).

Allocating a payment to an invoice is the one mutation the golden path performs, and it is
performed on the agent's recommendation. The rule here decides what the agent is permitted
to do alone, which under the shipped policy is nothing: allocation_authority_max is zero,
so every allocation goes to a person.

The authority threshold is still real rather than decorative — the policy value exists, so
the code path it controls exists and is tested. Setting it above zero is a deliberate act
with a tested consequence, not an undefined one.
"""

from decimal import Decimal

import pytest

from src.domain.allocation import (
    AllocationDecision,
    decide_allocation,
)

PAYMENT_AMOUNT = Decimal("4200.00")

# Shipped default: the agent may complete no allocation on its own (FR-012).
NO_AUTHORITY = Decimal("0")


def decide(status="UNALLOCATED", amount=PAYMENT_AMOUNT, authority=NO_AUTHORITY):
    return decide_allocation(
        payment_status=status,
        payment_amount=amount,
        authority_max=authority,
    )


class TestTheGoldenPath:
    def test_an_unallocated_payment_is_proposed_for_review(self):
        result = decide()
        assert result.decision is AllocationDecision.PROPOSE_REVIEW
        assert result.new_status == "UNDER_REVIEW"
        assert result.requires_human_approval is True

    def test_the_agent_never_allocates_directly_under_the_shipped_policy(self):
        """With authority at zero, no amount reaches the auto-allocation branch. This is the
        property the demo depends on: the agent recommends, a person decides."""
        for amount in ["0.01", "100.00", "4200.00", "999999.00"]:
            result = decide(amount=Decimal(amount))
            assert result.decision is AllocationDecision.PROPOSE_REVIEW
            assert result.new_status != "ALLOCATED"

    def test_the_previous_status_is_carried_for_the_audit_record(self):
        """FR-041 requires previous and new state on every audited action, and the rule is
        the only place that knows both."""
        result = decide()
        assert result.previous_status == "UNALLOCATED"
        assert result.new_status == "UNDER_REVIEW"


class TestIdempotence:
    def test_a_payment_already_under_review_is_not_proposed_twice(self):
        """A caller who repeats themselves, or a dropped call redialled, must not create a
        second review. The result carries the fact rather than an error, so the agent can
        say something true and calm (FR-022)."""
        result = decide(status="UNDER_REVIEW")
        assert result.decision is AllocationDecision.ALREADY_UNDER_REVIEW
        assert result.requires_human_approval is True

    def test_an_allocated_payment_is_already_resolved(self):
        result = decide(status="ALLOCATED")
        assert result.decision is AllocationDecision.ALREADY_ALLOCATED
        assert result.requires_human_approval is False


class TestIllegalTransitions:
    @pytest.mark.parametrize("status", ["PENDING", "FAILED", "REJECTED", "OPEN", "PAID"])
    def test_a_payment_not_in_an_allocatable_state_is_refused(self, status):
        """Only a settled, unallocated payment can be proposed. A pending payment may yet
        fail, and proposing it would put money under review that has not arrived."""
        result = decide(status=status)
        assert result.decision is AllocationDecision.NOT_ALLOWED
        assert result.new_status is None

    def test_an_unknown_status_is_refused_rather_than_assumed(self):
        """A status this code does not recognise means the ledger has moved on without it.
        Guessing is how a rule quietly stops enforcing anything."""
        result = decide(status="SOMETHING_NEW")
        assert result.decision is AllocationDecision.NOT_ALLOWED


class TestAuthorityThreshold:
    def test_an_amount_within_authority_may_be_allocated_directly(self):
        """Only reachable when someone raises the policy value deliberately."""
        result = decide(amount=Decimal("50.00"), authority=Decimal("100"))
        assert result.decision is AllocationDecision.AUTO_ALLOCATE
        assert result.new_status == "ALLOCATED"
        assert result.requires_human_approval is False

    def test_the_threshold_is_inclusive(self):
        result = decide(amount=Decimal("100.00"), authority=Decimal("100"))
        assert result.decision is AllocationDecision.AUTO_ALLOCATE

    def test_a_franc_above_authority_goes_to_review(self):
        result = decide(amount=Decimal("100.01"), authority=Decimal("100"))
        assert result.decision is AllocationDecision.PROPOSE_REVIEW

    def test_authority_never_applies_to_a_payment_in_the_wrong_state(self):
        """A generous threshold must not become a way to allocate a pending payment."""
        result = decide(status="PENDING", amount=Decimal("1.00"), authority=Decimal("100"))
        assert result.decision is AllocationDecision.NOT_ALLOWED

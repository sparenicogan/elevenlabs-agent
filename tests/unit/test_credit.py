"""The goodwill credit ceilings (FR-013b, FR-013d, FR-014).

Three limits apply to every request, and they exist for different reasons:

- **CHF 100 per request** bounds what a single call can give away.
- **CHF 500 over twelve months** stops the first limit being defeated by asking repeatedly.
- **The charge's own amount** stops a customer being credited more than they were billed —
  a data-integrity rule rather than a policy one, which is why it survives any change to
  the other two.

All three are inclusive. Boundaries are where rules actually break, so every one is tested
on both sides.
"""

from decimal import Decimal

import pytest
from src.domain.credit import (
    CreditableEntry,
    CreditOutcome,
    evaluate_credit,
)

from src.domain.risk import RiskLevel

MAX_PER_REQUEST = Decimal("100")
MAX_ROLLING = Decimal("500")


def entry(amount="4200.00", already_credited="0.00", has_open_dispute=False):
    """A charge a credit could be applied to. Defaults describe an ordinary invoice."""
    return CreditableEntry(
        entry_id="inv_1",
        amount=Decimal(amount),
        already_credited=Decimal(already_credited),
        has_open_dispute=has_open_dispute,
    )


def evaluate(
    amount="40.00",
    charge=None,
    account_status="ACTIVE",
    rolling_total="0.00",
    risk=RiskLevel.NONE,
):
    return evaluate_credit(
        requested_amount=Decimal(amount),
        entry=charge if charge is not None else entry(),
        account_status=account_status,
        rolling_total=Decimal(rolling_total),
        risk_level=risk,
        max_per_request=MAX_PER_REQUEST,
        max_rolling=MAX_ROLLING,
    )


class TestTheOrdinaryGrant:
    def test_a_small_credit_on_a_clean_account_is_granted(self):
        result = evaluate()
        assert result.outcome is CreditOutcome.GRANTED
        assert result.credit_amount == Decimal("40.00")

    def test_the_rolling_total_after_is_reported_so_the_agent_can_be_accurate(self):
        result = evaluate(amount="40.00", rolling_total="60.00")
        assert result.rolling_total_after == Decimal("100.00")

    def test_a_grant_names_the_rule_that_permitted_it(self):
        """Every audited action records its authorizing rule (FR-041), and 'it was allowed'
        is not a rule."""
        assert evaluate().authorizing_rule == "within_credit_limits"


class TestThePerRequestCeiling:
    def test_exactly_one_hundred_is_granted(self):
        """Inclusive. A rule that refuses its own stated limit confuses everyone."""
        assert evaluate(amount="100.00").outcome is CreditOutcome.GRANTED

    def test_one_centime_over_is_refused(self):
        result = evaluate(amount="100.01")
        assert result.outcome is CreditOutcome.DENIED_LIMIT
        assert result.authorizing_rule == "credit_max_per_request"

    def test_a_hundred_and_one_is_refused_whatever_the_history(self):
        assert evaluate(amount="101.00", rolling_total="0.00").outcome is CreditOutcome.DENIED_LIMIT


class TestTheRollingCeiling:
    def test_four_hundred_of_history_plus_a_hundred_lands_exactly_on_the_limit(self):
        result = evaluate(amount="100.00", rolling_total="400.00")
        assert result.outcome is CreditOutcome.GRANTED
        assert result.rolling_total_after == Decimal("500.00")

    def test_four_hundred_and_one_plus_a_hundred_exceeds_it(self):
        result = evaluate(amount="100.00", rolling_total="401.00")
        assert result.outcome is CreditOutcome.DENIED_LIMIT
        assert result.authorizing_rule == "credit_max_rolling"

    def test_a_small_request_is_refused_once_the_window_is_full(self):
        """The point of the cumulative rule: the per-request limit alone is defeated by
        asking repeatedly (FR-014)."""
        result = evaluate(amount="80.00", rolling_total="460.00")
        assert result.outcome is CreditOutcome.DENIED_LIMIT

    def test_a_customer_at_the_ceiling_can_still_be_granted_nothing_more(self):
        assert evaluate(amount="0.01", rolling_total="500.00").outcome is CreditOutcome.DENIED_LIMIT


class TestTheChargeCannotBeOvercredited:
    def test_a_credit_larger_than_the_charge_is_refused(self):
        """CHF 100 against a CHF 20 charge, even though 100 is within the per-request
        limit (FR-013d)."""
        result = evaluate(amount="100.00", charge=entry(amount="20.00"))
        assert result.outcome is CreditOutcome.DENIED_EXCEEDS_ENTRY
        assert result.authorizing_rule == "entry_amount_cap"

    def test_a_credit_equal_to_the_charge_is_allowed(self):
        assert (
            evaluate(amount="20.00", charge=entry(amount="20.00")).outcome is CreditOutcome.GRANTED
        )

    def test_earlier_credits_on_the_same_charge_reduce_what_is_left(self):
        result = evaluate(amount="40.00", charge=entry(amount="50.00", already_credited="30.00"))
        assert result.outcome is CreditOutcome.DENIED_EXCEEDS_ENTRY

    def test_a_further_credit_within_what_remains_is_allowed(self):
        """A prior credit on the same charge does not disqualify a later one (FR-013c)."""
        result = evaluate(amount="15.00", charge=entry(amount="50.00", already_credited="30.00"))
        assert result.outcome is CreditOutcome.GRANTED

    def test_the_entry_cap_is_checked_before_the_ceilings(self):
        """So the most specific reason is the one reported. 'You cannot be credited more than
        you were charged' tells a reviewer more than 'over the limit'."""
        result = evaluate(amount="100.01", charge=entry(amount="20.00"))
        assert result.outcome is CreditOutcome.DENIED_EXCEEDS_ENTRY


class TestEligibility:
    @pytest.mark.parametrize("status", ["SUSPENDED", "COLLECTIONS", "CLOSED"])
    def test_an_account_not_active_is_ineligible(self, status):
        result = evaluate(account_status=status)
        assert result.outcome is CreditOutcome.DENIED_INELIGIBLE
        assert result.authorizing_rule == "account_status"

    def test_a_charge_already_in_dispute_is_ineligible(self):
        """Crediting something a person is already reviewing would pre-empt their decision."""
        result = evaluate(charge=entry(has_open_dispute=True))
        assert result.outcome is CreditOutcome.DENIED_INELIGIBLE
        assert result.authorizing_rule == "open_dispute"

    def test_eligibility_is_checked_before_the_money_rules(self):
        """A request against a suspended account is refused for that reason, not for being
        over a limit it was never going to reach."""
        result = evaluate(amount="900.00", account_status="SUSPENDED")
        assert result.outcome is CreditOutcome.DENIED_INELIGIBLE


class TestRiskOverrides:
    def test_high_risk_refuses_a_request_that_would_otherwise_pass(self):
        """FR-015. The point of a risk check is that it can say no to something the rules
        would otherwise allow."""
        result = evaluate(risk=RiskLevel.HIGH)
        assert result.outcome is CreditOutcome.DENIED_RISK
        assert result.authorizing_rule == "risk_override"

    def test_elevated_risk_alone_does_not_refuse(self):
        """Something noticed is not something proven. Elevated is for the human to weigh."""
        assert evaluate(risk=RiskLevel.ELEVATED).outcome is CreditOutcome.GRANTED

    def test_risk_is_checked_after_eligibility(self):
        """A request that makes no sense is refused for that, not attributed to the caller's
        risk profile — which would put an unfair note on their record."""
        result = evaluate(account_status="CLOSED", risk=RiskLevel.HIGH)
        assert result.outcome is CreditOutcome.DENIED_INELIGIBLE

    def test_risk_is_checked_before_the_money_rules(self):
        result = evaluate(amount="900.00", risk=RiskLevel.HIGH)
        assert result.outcome is CreditOutcome.DENIED_RISK


class TestAmountsAreExact:
    def test_money_is_never_compared_as_a_float(self):
        """4200.00 has no exact binary representation. Every comparison here is Decimal, and
        a boundary case is exactly where a float would betray you."""
        result = evaluate(amount="100.00", rolling_total="400.00")
        assert result.outcome is CreditOutcome.GRANTED
        assert result.rolling_total_after == Decimal("500.00")

    @pytest.mark.parametrize("amount", ["0.00", "-10.00"])
    def test_a_request_of_nothing_or_less_is_refused(self, amount):
        """A negative credit is a charge, and the agent has no authority to bill anyone."""
        assert evaluate(amount=amount).outcome is CreditOutcome.DENIED_INELIGIBLE

"""What a refusal is allowed to say.

A customer refused a credit may be entirely honest. The rules that stopped them are about
patterns and totals, not about them, and the system does not know why the shape is there —
a company genuinely having a bad year produces the same history as one testing the limits.

So nothing the backend emits may carry an accusation, because everything it emits is read by
somebody: rule names reach the agent and shape how it speaks, evidence strings reach the
human picking up the ticket, and both end up in the audit record that would be produced if
the decision were ever challenged.

This is a linguistic test of machine-readable strings, which is unusual. It is here because
the words were chosen deliberately and a later rename could quietly undo that.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.domain.credit import CreditableEntry, evaluate_credit
from src.domain.risk import RiskLevel, SignalType, detect_history_signals

# Words that assert something about the person rather than the record. Some are outright
# accusations; others ("deliberate", "attempt") smuggle in intent that nobody has
# established.
ACCUSATORY = (
    "fraud",
    "fraudulent",
    "abuse",
    "abusive",
    "dishonest",
    "suspicious",
    "deliberate",
    "intentional",
    "gaming",
    "exploit",
    "attempt",
    "excessive",
    "too many",
    "refused before",
)


def contains_accusation(text: str) -> str | None:
    """Returns the first accusatory word found, or None."""
    lowered = text.lower()
    return next((word for word in ACCUSATORY if word in lowered), None)


class TestRuleNames:
    """Rule names reach the agent, which speaks from them."""

    @pytest.mark.parametrize(
        ("amount", "rolling", "risk"),
        [
            (Decimal("101"), Decimal("0"), RiskLevel.NONE),
            (Decimal("100"), Decimal("450"), RiskLevel.NONE),
            (Decimal("40"), Decimal("0"), RiskLevel.HIGH),
        ],
    )
    def test_no_refusal_names_a_rule_that_accuses_anyone(self, amount, rolling, risk):
        decision = evaluate_credit(
            requested_amount=amount,
            entry=CreditableEntry("inv_1", Decimal("4200"), Decimal("0"), False),
            account_status="ACTIVE",
            rolling_total=rolling,
            risk_level=risk,
            max_per_request=Decimal("100"),
            max_rolling=Decimal("500"),
        )
        found = contains_accusation(decision.authorizing_rule)
        assert found is None, f"rule '{decision.authorizing_rule}' contains '{found}'"

    def test_rule_names_describe_the_limit_rather_than_the_caller(self):
        """'credit_max_rolling' is a fact about the policy. 'customer_asks_too_often' would
        be a fact about a person the system has not established anything about."""
        decision = evaluate_credit(
            requested_amount=Decimal("100"),
            entry=CreditableEntry("inv_1", Decimal("4200"), Decimal("0"), False),
            account_status="ACTIVE",
            rolling_total=Decimal("450"),
            risk_level=RiskLevel.NONE,
            max_per_request=Decimal("100"),
            max_rolling=Decimal("500"),
        )
        assert decision.authorizing_rule == "credit_max_rolling"


class TestSignalEvidence:
    """Evidence strings reach the human who picks up the escalation, and the audit record."""

    def _history(self, count: int) -> list[dict]:
        today = date.today()
        return [
            {
                "type": "CREDIT_NOTE",
                "amount": Decimal("-90.00"),
                "entry_date": (today - timedelta(days=30 * (n + 1))).isoformat(),
                "status": "APPROVED",
            }
            for n in range(count)
        ]

    def test_no_signal_evidence_accuses_anyone(self):
        signals = detect_history_signals(
            ledger=self._history(5),
            conversations=[
                {"started_at": date.today().isoformat(), "outcome": "ESCALATED"} for _ in range(4)
            ],
            customer_id="CUST-1",
            conversation_id="conv_1",
            today=date.today(),
            max_per_request=Decimal("100"),
        )
        assert signals
        for signal in signals:
            found = contains_accusation(signal.evidence)
            assert found is None, f"'{signal.evidence}' contains '{found}'"

    def test_evidence_is_a_count_and_a_window(self):
        """Something a reviewer can check against the record, rather than a conclusion they
        have to take on trust."""
        signals = detect_history_signals(
            ledger=self._history(4),
            conversations=[],
            customer_id="CUST-1",
            conversation_id="conv_1",
            today=date.today(),
            max_per_request=Decimal("100"),
        )
        evidence = signals[0].evidence
        assert "4" in evidence
        assert "days" in evidence


class TestSignalNames:
    def test_signal_names_describe_what_was_seen_not_what_it_means(self):
        """'SUSPECTED_THRESHOLD_SPLITTING' names a shape and marks it as suspected.
        'FRAUD_DETECTED' would state a conclusion nothing here can support."""
        for signal in SignalType:
            assert "FRAUD" not in str(signal)
            assert "DETECTED" not in str(signal)

    def test_the_pattern_signal_is_marked_as_suspected(self):
        assert str(SignalType.SUSPECTED_THRESHOLD_SPLITTING).startswith("SUSPECTED_")

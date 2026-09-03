"""Detecting patterns across a customer's history (FR-016).

A single small credit is a customer having a bad week. Five of them, each just under the
limit that would require approval, is a shape — and the shape is the point. None of these
individual facts is suspicious, which is exactly why a person reading one call cannot see
them and a rule reading the history can.

Everything here is pure: given a history, produce signals. Whether a caller is then refused
is decided elsewhere, because "this looks unusual" and "this is not allowed" are different
statements and conflating them is how an honest customer ends up accused.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.domain.risk import (
    RiskLevel,
    RiskSignal,
    SignalType,
    detect_history_signals,
    evaluate,
)

TODAY = date(2026, 9, 3)
CONVERSATION = "conv_1"
CUSTOMER = "CUST-01144"

# The per-request ceiling. A credit close to it, repeatedly, is the shape that matters.
MAX_PER_REQUEST = Decimal("100")


def credit(amount: str, days_ago: int) -> dict:
    return {
        "type": "CREDIT_NOTE",
        "amount": Decimal(f"-{amount}"),
        "entry_date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "status": "APPROVED",
    }


def conversation(days_ago: int, outcome: str = "RESOLVED_AUTONOMOUS") -> dict:
    return {
        "started_at": (TODAY - timedelta(days=days_ago)).isoformat(),
        "outcome": outcome,
    }


def detect(ledger=None, conversations=None):
    return detect_history_signals(
        ledger=ledger or [],
        conversations=conversations or [],
        customer_id=CUSTOMER,
        conversation_id=CONVERSATION,
        today=TODAY,
        max_per_request=MAX_PER_REQUEST,
    )


def types(signals: list[RiskSignal]) -> set[str]:
    return {str(s.signal_type) for s in signals}


class TestThresholdSplitting:
    """Credits clustered just below the limit that would need approval."""

    def test_five_credits_near_the_limit_is_a_pattern(self):
        """Dubois: 90, 95, 100, 85, 90 — none of them individually remarkable."""
        ledger = [
            credit("90.00", 300),
            credit("95.00", 210),
            credit("100.00", 150),
            credit("85.00", 90),
            credit("90.00", 30),
        ]
        assert SignalType.SUSPECTED_THRESHOLD_SPLITTING in {s.signal_type for s in detect(ledger)}

    def test_two_credits_near_the_limit_is_not_yet_a_pattern(self):
        """Two is a coincidence. Treating it as a pattern would flag ordinary customers."""
        ledger = [credit("90.00", 200), credit("95.00", 60)]
        assert SignalType.SUSPECTED_THRESHOLD_SPLITTING not in types(detect(ledger))

    def test_small_credits_well_below_the_limit_are_not_the_pattern(self):
        """Someone taking CHF 5 four times is not working around a CHF 100 ceiling."""
        ledger = [credit("5.00", 300), credit("4.00", 200), credit("6.00", 100), credit("5.00", 20)]
        assert SignalType.SUSPECTED_THRESHOLD_SPLITTING not in types(detect(ledger))

    def test_credits_outside_the_window_do_not_count(self):
        ledger = [
            credit("90.00", 500),
            credit("95.00", 480),
            credit("100.00", 450),
            credit("90.00", 30),
        ]
        assert SignalType.SUSPECTED_THRESHOLD_SPLITTING not in types(detect(ledger))

    def test_the_evidence_states_the_shape_without_accusing_anyone(self):
        ledger = [credit("90.00", 300), credit("95.00", 210), credit("100.00", 150)]
        signal = next(
            s for s in detect(ledger) if s.signal_type is SignalType.SUSPECTED_THRESHOLD_SPLITTING
        )
        assert "3" in signal.evidence
        for word in ("fraud", "abuse", "dishonest", "suspicious", "deliberate"):
            assert word not in signal.evidence.lower()


class TestContactFrequency:
    def test_many_calls_in_a_short_period_is_a_signal(self):
        conversations = [conversation(days_ago) for days_ago in (1, 3, 5, 8, 12, 20)]
        assert SignalType.HIGH_CONTACT_FREQUENCY in types(detect(conversations=conversations))

    def test_ordinary_contact_is_not(self):
        conversations = [conversation(10), conversation(45)]
        assert SignalType.HIGH_CONTACT_FREQUENCY not in types(detect(conversations=conversations))

    def test_old_calls_do_not_count(self):
        conversations = [conversation(days_ago) for days_ago in (200, 210, 220, 230, 240, 250)]
        assert SignalType.HIGH_CONTACT_FREQUENCY not in types(detect(conversations=conversations))


class TestRepeatedDisputes:
    def test_several_escalated_calls_is_a_signal(self):
        conversations = [conversation(d, outcome="ESCALATED") for d in (20, 60, 120)]
        assert SignalType.REPEATED_DISPUTES in types(detect(conversations=conversations))

    def test_one_escalation_is_a_customer_with_a_problem(self):
        """Not a pattern. A customer who escalated once and had it resolved is a customer
        who was helped."""
        conversations = [conversation(20, outcome="ESCALATED"), conversation(60)]
        assert SignalType.REPEATED_DISPUTES not in types(detect(conversations=conversations))


class TestUnusualPaymentBehaviour:
    def test_several_payments_that_never_allocated_is_a_signal(self):
        ledger = [
            {
                "type": "PAYMENT",
                "status": "UNALLOCATED",
                "amount": Decimal("-100.00"),
                "entry_date": (TODAY - timedelta(days=d)).isoformat(),
            }
            for d in (10, 40, 80)
        ]
        assert SignalType.UNUSUAL_PAYMENT_BEHAVIOUR in types(detect(ledger))

    def test_one_unallocated_payment_is_the_ordinary_case(self):
        """The golden path is exactly this. It must not raise a signal, or every disputed
        invoice call would start with the caller flagged."""
        ledger = [
            {
                "type": "PAYMENT",
                "status": "UNALLOCATED",
                "amount": Decimal("-4200.00"),
                "entry_date": TODAY.isoformat(),
            }
        ]
        assert SignalType.UNUSUAL_PAYMENT_BEHAVIOUR not in types(detect(ledger))


class TestSignalsCarryTheirContext:
    def test_every_signal_names_the_customer_and_the_call(self):
        ledger = [credit("90.00", 300), credit("95.00", 210), credit("100.00", 150)]
        for signal in detect(ledger):
            assert signal.customer_id == CUSTOMER
            assert signal.conversation_id == CONVERSATION

    def test_a_clean_history_produces_nothing(self):
        assert detect() == []

    def test_no_signal_evidence_contains_a_stored_value(self):
        """Evidence is counts and comparisons, never amounts or dates a caller could be
        confronted with (FR-016)."""
        ledger = [credit("90.00", 300), credit("95.00", 210), credit("100.00", 150)]
        for signal in detect(ledger):
            assert "90.00" not in signal.evidence
            assert "2026" not in signal.evidence


class TestAggregation:
    def test_one_accumulating_signal_is_elevated_not_high(self):
        signals = [RiskSignal(SignalType.HIGH_CONTACT_FREQUENCY, "6 calls", CONVERSATION)]
        assert evaluate(signals) is RiskLevel.ELEVATED

    def test_two_accumulating_signals_reach_high(self):
        signals = [
            RiskSignal(SignalType.HIGH_CONTACT_FREQUENCY, "6 calls", CONVERSATION),
            RiskSignal(SignalType.REPEATED_DISPUTES, "3 escalations", CONVERSATION),
        ]
        assert evaluate(signals) is RiskLevel.HIGH

    @pytest.mark.parametrize(
        "decisive",
        [
            SignalType.SUSPECTED_GUESSING,
            SignalType.REPEATED_FAILED_VERIFICATION,
            SignalType.CONFLICTING_IDENTITY_DATA,
            SignalType.SUSPECTED_THRESHOLD_SPLITTING,
        ],
    )
    def test_a_decisive_signal_is_high_on_its_own(self, decisive):
        assert evaluate([RiskSignal(decisive, "evidence", CONVERSATION)]) is RiskLevel.HIGH

    def test_no_signals_is_no_risk(self):
        assert evaluate([]) is RiskLevel.NONE

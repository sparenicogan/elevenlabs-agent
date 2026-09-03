"""Doing the same thing twice must not have happened twice (FR-022).

Voice calls drop, callers repeat themselves, and a language model will occasionally call a
tool a second time for reasons of its own. None of those may cost a customer a second credit
or a second review.

The keys are deterministic rather than random precisely so that a repeat collides. That is
the whole mechanism: the write is conditional, the duplicate loses, and the caller is told
the same thing they were told the first time.
"""

from src.common import idempotency


class TestTheKeyIsDerivedFromTheRequest:
    def test_the_same_request_produces_the_same_key(self):
        assert idempotency.key("conv_1", "pay_1", "allocate") == idempotency.key(
            "conv_1", "pay_1", "allocate"
        )

    def test_a_different_conversation_is_a_different_request(self):
        assert idempotency.key("conv_1", "pay_1", "allocate") != idempotency.key(
            "conv_2", "pay_1", "allocate"
        )

    def test_a_different_entry_is_a_different_request(self):
        assert idempotency.key("conv_1", "pay_1", "allocate") != idempotency.key(
            "conv_1", "pay_2", "allocate"
        )

    def test_a_different_action_on_the_same_entry_is_a_different_request(self):
        assert idempotency.key("conv_1", "inv_1", "credit") != idempotency.key(
            "conv_1", "inv_1", "allocate"
        )

    def test_the_order_of_the_parts_matters(self):
        """Otherwise a conversation id and an entry id could swap places and collide."""
        assert idempotency.key("a", "b") != idempotency.key("b", "a")


class TestTheKeyRevealsNothing:
    def test_the_inputs_cannot_be_read_out_of_it(self):
        key = idempotency.key("conv_1", "CUST-00417", "credit")
        assert "CUST-00417" not in key
        assert "conv_1" not in key

    def test_it_is_a_fixed_length_however_many_parts_go_in(self):
        """Hashed rather than concatenated, so an action needing five identifiers produces a
        key the same size as one needing two."""
        assert len(idempotency.key("a")) == len(idempotency.key("a", "b", "c", "d", "e"))


class TestTheConditionalWriteIsTheMechanism:
    def test_put_if_absent_reports_a_duplicate_rather_than_raising(self):
        """A duplicate is a normal outcome, not an error. The handler returns the original
        result so a retry is indistinguishable from a first call to the person on the phone.
        """
        import inspect

        from src.adapters import dynamo

        source = inspect.getsource(dynamo.put_if_absent)
        assert "ConditionalCheckFailedException" in source
        assert "return False" in source

    def test_update_if_reports_a_lost_race_rather_than_raising(self):
        import inspect

        from src.adapters import dynamo

        source = inspect.getsource(dynamo.update_if)
        assert "ConditionalCheckFailedException" in source
        assert "return None" in source

    def test_a_credit_id_is_derived_from_the_request(self):
        """So the same request writes the same row, and the second write loses its
        condition rather than creating a second credit."""
        from decimal import Decimal

        from src.handlers.request_credit import _credit_id

        first = _credit_id("conv_1", "inv_1", Decimal("40.00"))
        second = _credit_id("conv_1", "inv_1", Decimal("40.00"))
        assert first == second
        assert first.startswith("cn_")

    def test_a_different_amount_is_a_different_credit(self):
        from decimal import Decimal

        from src.handlers.request_credit import _credit_id

        assert _credit_id("conv_1", "inv_1", Decimal("40.00")) != _credit_id(
            "conv_1", "inv_1", Decimal("50.00")
        )

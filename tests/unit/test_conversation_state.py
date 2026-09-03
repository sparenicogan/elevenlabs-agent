"""Server-side conversation state — where the disclosure gate actually lives.

Every financial tool asks this module whether the caller is verified, so a write that
silently does nothing here produces a system that refuses verified callers while looking
like it is working correctly. That is exactly what happened: set_verification updated the
conversation record conditionally on it already existing, and nothing created it until the
initiation webhook was built. Verification succeeded, the write was discarded, and every
subsequent tool refused.

The contract tests could not catch it because they mock set_verification, so these exercise
the module itself.
"""

import pytest

from src.common import conversation_state
from src.common.conversation_state import VerificationStatus


@pytest.fixture
def dynamo(mocker):
    """Replaces the DynamoDB adapter and records how it was called."""
    return mocker.patch.object(conversation_state, "dynamo")


class TestVerificationSurvivesAMissingRecord:
    def test_setting_verification_does_not_require_an_existing_record(self, dynamo):
        """The regression. A conditional write is discarded when the record is absent, and a
        discarded write here is indistinguishable from the gate working."""
        conversation_state.set_verification("conv_1", VerificationStatus.VERIFIED, "CUST-1")

        dynamo.upsert.assert_called_once()
        dynamo.update_if.assert_not_called()

    def test_the_write_carries_no_condition_that_could_discard_it(self, dynamo):
        conversation_state.set_verification("conv_1", VerificationStatus.VERIFIED, "CUST-1")

        _, kwargs = dynamo.upsert.call_args
        assert "ConditionExpression" not in kwargs
        assert "condition" not in kwargs

    def test_the_customer_and_status_are_both_recorded(self, dynamo):
        conversation_state.set_verification("conv_1", VerificationStatus.VERIFIED, "CUST-1")

        values = dynamo.upsert.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":s"] == "VERIFIED"
        assert values[":c"] == "CUST-1"

    def test_an_existing_start_time_is_not_overwritten(self, dynamo):
        """A call that did pass through the initiation webhook keeps its real start time;
        one that did not gets a usable approximation rather than nothing."""
        conversation_state.set_verification("conv_1", VerificationStatus.VERIFIED, "CUST-1")

        expression = dynamo.upsert.call_args.kwargs["UpdateExpression"]
        assert "if_not_exists(started_at" in expression


class TestSessionAttemptCounting:
    def test_counting_a_failure_does_not_require_an_existing_record(self, dynamo):
        """Same hazard: a caller who never names an account has no customer counter, so the
        session counter is the only thing bounding their guessing (FR-006)."""
        dynamo.upsert.return_value = {"failed_verification_attempts": 1}

        assert conversation_state.record_failed_attempt("conv_1") == 1
        dynamo.update_if.assert_not_called()

    def test_the_count_comes_back_from_the_write(self, dynamo):
        dynamo.upsert.return_value = {"failed_verification_attempts": 3}

        assert conversation_state.record_failed_attempt("conv_1") == 3

    def test_an_absent_count_is_treated_as_the_first_attempt(self, dynamo):
        dynamo.upsert.return_value = None

        assert conversation_state.record_failed_attempt("conv_1") == 1


class TestTheGate:
    def test_a_missing_conversation_is_not_verified(self, dynamo):
        from src.adapters.errors import ToolError

        dynamo.get.return_value = None
        with pytest.raises(ToolError):
            conversation_state.require_verified("conv_unknown")

    def test_partially_verified_grants_nothing(self, dynamo):
        """PARTIALLY_VERIFIED means keep asking, not proceed carefully. Verification is a
        single uniform bar of three factors."""
        from src.adapters.errors import ToolError

        dynamo.get.return_value = {
            "conversation_id": "conv_1",
            "verification_status": "PARTIALLY_VERIFIED",
            "customer_id": "CUST-1",
        }
        with pytest.raises(ToolError):
            conversation_state.require_verified("conv_1")

    def test_a_verified_conversation_returns_its_customer(self, dynamo):
        dynamo.get.return_value = {
            "conversation_id": "conv_1",
            "verification_status": "VERIFIED",
            "customer_id": "CUST-00417",
        }
        assert conversation_state.require_verified("conv_1") == "CUST-00417"

    def test_the_customer_is_taken_from_the_record_not_from_the_caller(self, dynamo):
        """There is no parameter a caller could use to name someone else's account: the
        customer is whatever verification resolved and stored."""
        dynamo.get.return_value = {
            "conversation_id": "conv_1",
            "verification_status": "VERIFIED",
            "customer_id": "CUST-99999",
        }
        assert conversation_state.require_verified("conv_1") == "CUST-99999"

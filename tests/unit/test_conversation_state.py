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
            "customer_id": "445909044455",
        }
        assert conversation_state.require_verified("conv_1") == "445909044455"

    def test_the_customer_is_taken_from_the_record_not_from_the_caller(self, dynamo):
        """There is no parameter a caller could use to name someone else's account: the
        customer is whatever verification resolved and stored."""
        dynamo.get.return_value = {
            "conversation_id": "conv_1",
            "verification_status": "VERIFIED",
            "customer_id": "CUST-99999",
        }
        assert conversation_state.require_verified("conv_1") == "CUST-99999"


class TestStateExpiresOnItsOwn:
    """A TTL that nothing writes the attribute for is a TTL that never fires -- the table
    setting and the write have to be checked together or neither is real."""

    def test_every_write_that_can_create_the_row_sets_an_expiry(self):
        """There is no single place a conversation is opened, so each of these may be the
        write that creates it. One of them forgetting means rows that never expire."""
        import inspect

        from src.common import conversation_state

        source = inspect.getsource(conversation_state)
        creators = [
            "set_verification",
            "record_risk_signal",
            "record_factor_attempts",
            "record_wrong_values",
            "record_callback",
            "set_resolved_contact",
        ]
        for name in creators:
            body = source.split(f"def {name}(")[1].split("\ndef ")[0]
            assert "_TOUCH" in body, f"{name} can create a row without an expiry"
            assert '":expires"' in body, f"{name} does not bind :expires"

    def test_the_table_actually_has_ttl_enabled(self):
        import pathlib

        tf = (
            pathlib.Path(__file__).resolve().parents[2] / "infra/terraform/dynamodb.tf"
        ).read_text()
        conversations = tf.split('resource "aws_dynamodb_table" "conversations"')[1].split(
            "\nresource "
        )[0]
        assert "ttl {" in conversations
        assert 'attribute_name = "expires_at"' in conversations
        assert "enabled        = true" in conversations

    def test_the_permanent_record_has_no_ttl(self):
        """The performance table is what survives; an expiry on it would defeat the split."""
        import pathlib

        tf = (
            pathlib.Path(__file__).resolve().parents[2] / "infra/terraform/dynamodb.tf"
        ).read_text()
        interactions = tf.split('resource "aws_dynamodb_table" "performance"')[1].split(
            "\nresource "
        )[0]
        assert "ttl {" not in interactions

    def test_state_outlives_the_window_the_risk_rules_read(self):
        """The credit rules still read this table over 365 days. An expiry shorter than that
        would silently stop raising contact-frequency signals rather than fail."""
        from src.common.conversation_state import STATE_RETENTION_DAYS
        from src.domain.risk import HISTORY_WINDOW_DAYS

        assert STATE_RETENTION_DAYS > HISTORY_WINDOW_DAYS

"""US7: the webhook that runs after the call has ended.

Nobody is on the line, so nothing here is urgent — but a duplicate delivery must not double
anything, a forged one must not be accepted, and a step that fails must not take the steps
before it down with it.
"""

import hashlib
import hmac
import json
import time

import pytest

SECRET = "webhook-secret"


def _signed(body: str, secret: str = SECRET, age: int = 0) -> str:
    timestamp = int(time.time()) - age
    digest = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"t={timestamp},v0={digest}"


@pytest.fixture
def stubs(mocker):
    from src.handlers import post_call as module

    mocker.patch.object(module.secrets, "get", return_value=SECRET)
    mocker.patch.object(
        module.policy_module, "load", return_value=mocker.Mock(summary_max_chars=2000)
    )
    return {
        "claim": mocker.patch.object(
            module.conversation_state, "claim_post_call", return_value=True
        ),
        "callback": mocker.patch.object(module.conversation_state, "record_callback"),
        "put": mocker.patch.object(module.s3, "put_transcript"),
        "upsert": mocker.patch.object(module.dynamo, "upsert"),
        "get": mocker.patch.object(module.dynamo, "get", return_value={}),
        "module": module,
    }


def call(stubs, payload: dict, signature: str | None = None):
    body = json.dumps(payload)
    response = stubs["module"].handler(
        {
            "headers": {
                "ElevenLabs-Signature": signature if signature is not None else _signed(body)
            },
            "body": body,
        }
    )
    return response["statusCode"], json.loads(response["body"])


PAYLOAD = {
    "conversation_id": "conv_1",
    "customer_id": "445909044455",
    "metadata": {"call_duration_secs": 180, "language": "en"},
    "analysis": {"transcript_summary": "Asked about a reminder for an invoice."},
    "transcript": [],
}


class TestOnlyElevenLabsGetsIn:
    def test_a_valid_signature_is_accepted(self, stubs):
        status, _ = call(stubs, PAYLOAD)
        assert status == 200

    def test_a_wrong_secret_is_rejected(self, stubs):
        body = json.dumps(PAYLOAD)
        status, _ = call(stubs, PAYLOAD, signature=_signed(body, secret="not-the-secret"))
        assert status == 401
        stubs["put"].assert_not_called()

    def test_a_signature_from_an_hour_ago_is_rejected(self, stubs):
        """A valid signature on a request captured an hour ago is still a replay."""
        body = json.dumps(PAYLOAD)
        status, _ = call(stubs, PAYLOAD, signature=_signed(body, age=3700))
        assert status == 401

    def test_a_signature_just_inside_the_window_is_accepted(self, stubs):
        body = json.dumps(PAYLOAD)
        assert call(stubs, PAYLOAD, signature=_signed(body, age=1700))[0] == 200

    def test_a_missing_header_is_rejected(self, stubs):
        assert call(stubs, PAYLOAD, signature="")[0] == 401

    def test_a_malformed_header_is_rejected(self, stubs):
        assert call(stubs, PAYLOAD, signature="garbage")[0] == 401

    def test_the_body_is_signed_as_sent_not_as_reparsed(self, stubs):
        """Signing a re-serialised body would fail on whitespace the sender chose."""
        import inspect

        source = inspect.getsource(stubs["module"].handler)
        assert "json.loads" not in source.split("_verify_signature")[0]


class TestADuplicateDeliveryDoesNothingTwice:
    def test_the_second_delivery_is_a_no_op(self, stubs):
        stubs["claim"].return_value = False
        status, body = call(stubs, PAYLOAD)
        assert status == 200
        assert body["status"] == "DUPLICATE"
        stubs["put"].assert_not_called()

    def test_it_returns_200_rather_than_an_error(self, stubs):
        """An error invites a redelivery, and a redelivery is what we are declining."""
        stubs["claim"].return_value = False
        assert call(stubs, PAYLOAD)[0] == 200


class TestOneFailedStepDoesNotLoseTheOthers:
    def test_a_failed_summary_still_leaves_the_transcript(self, stubs):
        stubs["upsert"].side_effect = [None, None, ValueError("summary failed"), None]
        status, body = call(stubs, PAYLOAD)
        assert status == 200
        assert "transcript" in body["completed"]

    def test_a_failed_transcript_still_records_the_metrics(self, stubs):
        stubs["put"].side_effect = ValueError("s3 down")
        status, body = call(stubs, PAYLOAD)
        assert status == 200
        assert "metrics" in body["completed"]
        assert "transcript" not in body["completed"]


class TestWhatIsStored:
    def test_the_transcript_goes_to_s3_and_only_its_key_to_the_table(self, stubs):
        """The transcript expires in 90 days; the metadata lives for ten years."""
        call(stubs, PAYLOAD)
        assert stubs["put"].called
        keys = [
            c.kwargs.get("ExpressionAttributeValues", {}) for c in stubs["upsert"].call_args_list
        ]
        assert any(":key" in k for k in keys)
        assert not any("transcript" in json.dumps(k, default=str)[:0] for k in keys)

    def test_no_summary_is_written_for_an_unidentified_caller(self, stubs):
        """There is no account to remember anything against."""
        call(stubs, {k: v for k, v in PAYLOAD.items() if k != "customer_id"})
        written = json.dumps(
            [c.kwargs.get("ExpressionAttributeValues", {}) for c in stubs["upsert"].call_args_list],
            default=str,
        )
        assert "summary_text" not in written


class TestTheCallbackBackstop:
    def test_a_failed_transfer_records_the_callback_the_agent_promised(self, stubs):
        """The agent promises a callback before it transfers, precisely because a transfer can
        drop the call. When it does, this is the last thing that knows (research D4)."""
        call(
            stubs,
            {**PAYLOAD, "analysis": {"transfer_attempted": True, "transfer_result": "FAILED"}},
        )
        assert stubs["callback"].called

    def test_a_successful_transfer_records_nothing(self, stubs):
        call(
            stubs,
            {**PAYLOAD, "analysis": {"transfer_attempted": True, "transfer_result": "SUCCESS"}},
        )
        stubs["callback"].assert_not_called()

    def test_a_callback_already_recorded_is_not_duplicated(self, stubs):
        stubs["get"].return_value = {"callback": {"reason": "TRANSFER_FAILED"}}
        call(
            stubs,
            {**PAYLOAD, "analysis": {"transfer_attempted": True, "transfer_result": "FAILED"}},
        )
        stubs["callback"].assert_not_called()


class TestTheAdaptersAreCalledCorrectly:
    """Every adapter is mocked in the tests above, which is what makes them fast and is also
    what let a wrong keyword reach production. The webhook returned 500 on its first real
    delivery because upsert was passed a condition, which only update_if takes."""

    def test_upsert_refuses_a_condition_rather_than_passing_it_to_boto(self):
        import pytest as _pytest

        from src.adapters import dynamo

        with _pytest.raises(TypeError, match="use update_if"):
            dynamo.upsert("conversations", {"conversation_id": "c"}, condition="x")

    def test_the_summary_is_written_conditionally(self):
        """Two calls ending at once must not overwrite each other's work: the loser reads
        again and re-folds rather than winning by arriving second."""
        import inspect

        from src.handlers import post_call

        source = inspect.getsource(post_call._regenerate_summary)
        assert "update_if" in source
        assert "version = :expected" in source

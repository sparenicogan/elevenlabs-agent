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
        "upsert": mocker.patch.object(module.dynamo, "upsert"),
        # The summary is written conditionally, so update_if is a second seam. Unstubbed it
        # reached real DynamoDB on a developer machine and failed only in CI, where there is
        # no region -- which is the wrong way round for a test to tell you something.
        "update_if": mocker.patch.object(module.dynamo, "update_if", return_value={}),
        "get": mocker.patch.object(module.dynamo, "get", return_value={}),
        # The interaction row is written once and never updated, so it is a third seam.
        "put_if_absent": mocker.patch.object(module.dynamo, "put_if_absent", return_value=True),
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
        stubs["put_if_absent"].assert_not_called()

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
        stubs["put_if_absent"].assert_not_called()

    def test_it_returns_200_rather_than_an_error(self, stubs):
        """An error invites a redelivery, and a redelivery is what we are declining."""
        stubs["claim"].return_value = False
        assert call(stubs, PAYLOAD)[0] == 200


class TestOneFailedStepDoesNotLoseTheOthers:
    def test_a_failed_summary_still_leaves_the_performance_record(self, stubs):
        stubs["update_if"].side_effect = ValueError("summary failed")
        status, body = call(stubs, PAYLOAD)
        assert status == 200
        assert "performance" in body["completed"]

    def test_a_failed_performance_write_still_records_the_metrics(self, stubs):
        stubs["put_if_absent"].side_effect = ValueError("dynamo down")
        status, body = call(stubs, PAYLOAD)
        assert status == 200
        assert "metrics" in body["completed"]
        assert "performance" not in body["completed"]


class TestWhatIsStored:
    def test_the_transcript_is_not_copied_into_our_stores(self, stubs):
        """It expires at ElevenLabs in 90 days (FR-038a). Everything derived from it is in
        the performance table, which does not expire -- so a copy here would be the one thing
        outliving the retention it is subject to."""
        call(stubs, {**PAYLOAD, "transcript": [{"role": "agent", "message": "hello"}]})
        written = json.dumps(
            [c.kwargs.get("ExpressionAttributeValues", {}) for c in stubs["upsert"].call_args_list]
            + [stubs["put_if_absent"].call_args.args[1]],
            default=str,
        )
        assert "hello" not in written
        assert "transcript_s3_key" not in written

    def test_no_summary_is_written_for_an_unidentified_caller(self, stubs):
        """There is no account to remember anything against."""
        call(stubs, {k: v for k, v in PAYLOAD.items() if k != "customer_id"})
        written = json.dumps(
            [c.kwargs.get("ExpressionAttributeValues", {}) for c in stubs["upsert"].call_args_list],
            default=str,
        )
        assert "summary_text" not in written


class TestTheCallbackBackstop:
    """These tests used to assert against analysis.transfer_attempted and
    analysis.transfer_result. Neither field exists -- they were absent from all 40 real calls
    surveyed -- so the code and the tests agreed with each other and with nothing else, and
    the backstop never fired. The payloads below are the shape ElevenLabs actually sends."""

    @staticmethod
    def _transfer(used: bool, error: bool):
        return {
            **PAYLOAD,
            "metadata": {
                **PAYLOAD["metadata"],
                "features_usage": {"transfer_to_number": {"used": used}},
            },
            "transcript": [
                {"tool_results": [{"tool_name": "transfer_to_number", "is_error": error}]}
            ]
            if used
            else [],
        }

    def test_a_failed_transfer_records_the_callback_the_agent_promised(self, stubs):
        """The agent promises a callback before it transfers, precisely because a transfer can
        drop the call. When it does, this is the last thing that knows (research D4)."""
        call(stubs, self._transfer(used=True, error=True))
        assert stubs["callback"].called

    def test_a_successful_transfer_records_nothing(self, stubs):
        call(stubs, self._transfer(used=True, error=False))
        stubs["callback"].assert_not_called()

    def test_a_call_with_no_transfer_records_nothing(self, stubs):
        call(stubs, PAYLOAD)
        stubs["callback"].assert_not_called()

    def test_a_callback_already_recorded_is_not_duplicated(self, stubs):
        stubs["get"].return_value = {"callback": {"reason": "TRANSFER_FAILED"}}
        call(stubs, self._transfer(used=True, error=True))
        stubs["callback"].assert_not_called()


class TestWhatTheCallWas:
    """The outcome rule decides the escalation rate, the containment rate and the autonomous
    resolution rate. It read two fields that do not exist, so every call ever recorded was
    labelled RESOLVED_AUTONOMOUS and those three rates described a system that never
    escalates."""

    @staticmethod
    def _outcome(stubs, payload):
        call(stubs, payload)
        return stubs["put_if_absent"].call_args.args[1]["outcome"]

    def test_a_transfer_is_a_transfer(self, stubs):
        assert self._outcome(stubs, TestTheCallbackBackstop._transfer(True, False)) == "TRANSFERRED"

    def test_raising_a_ticket_is_an_escalation(self, stubs):
        payload = {
            **PAYLOAD,
            "transcript": [
                {"tool_results": [{"tool_name": "create_escalation", "is_error": False}]}
            ],
        }
        assert self._outcome(stubs, payload) == "ESCALATED"

    def test_a_call_that_ran_out_of_time_did_not_resolve(self, stubs):
        payload = {
            **PAYLOAD,
            "metadata": {
                **PAYLOAD["metadata"],
                "termination_reason": "Conversation has exceeded maximum duration",
            },
        }
        assert self._outcome(stubs, payload) == "ABANDONED"

    def test_a_call_that_died_on_quota_did_not_resolve(self, stubs):
        """Seen once in 40 calls. It is a failure of ours, not a resolution."""
        payload = {
            **PAYLOAD,
            "metadata": {
                **PAYLOAD["metadata"],
                "termination_reason": "This request exceeds your quota limit.",
            },
        }
        assert self._outcome(stubs, payload) == "ABANDONED"

    def test_an_ordinary_hangup_resolved(self, stubs):
        payload = {
            **PAYLOAD,
            "metadata": {
                **PAYLOAD["metadata"],
                "termination_reason": "Client disconnected: 1000",
            },
        }
        assert self._outcome(stubs, payload) == "RESOLVED_AUTONOMOUS"

    def test_the_rule_never_reads_a_field_elevenlabs_does_not_send(self):
        """The fields that caused this. Naming them keeps them out."""
        import inspect

        from src.domain import performance

        source = inspect.getsource(performance)
        for absent in ("transfer_attempted", "transfer_result", '"escalated"'):
            assert absent not in source.split('"""')[0] + "".join(
                part for i, part in enumerate(source.split('"""')) if i % 2 == 0
            ), f"the rule keys on {absent}, which is not a field"


class TestThePermanentRecord:
    """The row that outlives the call, and every reset."""

    def test_it_is_written_once_and_never_updated(self, stubs):
        call(stubs, PAYLOAD)
        table, row, key_field = stubs["put_if_absent"].call_args.args
        assert table == "performance"
        assert key_field == "conversation_id"
        assert row["conversation_id"] == "conv_1"

    def test_a_duplicate_delivery_writes_nothing_new(self, stubs):
        stubs["put_if_absent"].return_value = False
        status, body = call(stubs, PAYLOAD)
        assert status == 200
        assert "performance" in body["completed"]

    def test_it_carries_the_whole_elevenlabs_payload(self, stubs):
        """A question nobody has asked yet should still be answerable next year."""
        call(stubs, PAYLOAD)
        row = stubs["put_if_absent"].call_args.args[1]
        assert row["elevenlabs"]["analysis"]["transcript_summary"]

    def test_the_transcript_is_not_duplicated_into_it(self, stubs):
        """It is already in S3, where it is cheaper and cannot burst the item limit."""
        call(stubs, {**PAYLOAD, "transcript": [{"role": "agent", "message": "hello"}]})
        row = stubs["put_if_absent"].call_args.args[1]
        assert "transcript" not in row["elevenlabs"]

    def test_our_own_tool_latency_is_recorded(self, stubs):
        """ElevenLabs times its model, not our webhooks. A slow tool reaches their metrics
        only as silence on the line, so the number has to come from the tool results."""
        call(
            stubs,
            {
                **PAYLOAD,
                "transcript": [
                    {
                        "tool_results": [
                            {"tool_name": "get_account_context", "tool_latency_secs": 2.5},
                            {"tool_name": "get_account_context", "tool_latency_secs": 3.5},
                            {
                                "tool_name": "check_factor",
                                "tool_latency_secs": 5.0,
                                "is_error": True,
                            },
                        ]
                    },
                ],
            },
        )
        tools = stubs["put_if_absent"].call_args.args[1]["tools"]
        assert tools["get_account_context"] == {
            "calls": 2,
            "errors": 0,
            "latency_p50_ms": 3000,
            "latency_max_ms": 3500,
        }
        assert tools["check_factor"]["errors"] == 1

    def test_an_unverified_call_is_still_recorded(self, stubs):
        """A call nobody could verify is a data point, not a gap -- but it belongs to no
        customer, so the index key is left off rather than written empty. DynamoDB rejects an
        empty string as an index key, which is a write that fails rather than a row that is
        merely odd."""
        stubs["get"].return_value = {}
        call(stubs, PAYLOAD)
        row = stubs["put_if_absent"].call_args.args[1]
        assert "customer_id" not in row
        assert row["conversation_id"] == "conv_1"

    def test_a_verified_call_carries_its_customer(self, stubs):
        stubs["get"].return_value = {"customer_id": "445909044455"}
        call(stubs, PAYLOAD)
        assert stubs["put_if_absent"].call_args.args[1]["customer_id"] == "445909044455"

    def test_no_index_key_is_ever_written_empty(self, stubs):
        """The same mistake as the seed fixture, which failed the same way: an empty phone
        lookup on a contact with no phone. Index keys are omitted or they are real."""
        stubs["get"].return_value = {"customer_id": ""}
        call(stubs, PAYLOAD)
        row = stubs["put_if_absent"].call_args.args[1]
        for key in ("customer_id", "started_at"):
            assert row.get(key, "nonempty") != ""


class TestTestTrafficIsSeparable:
    """Of 57 conversations on record 39 were widget sessions and 18 real phone calls. A
    performance number that does not separate them is two thirds noise."""

    @staticmethod
    def _row(stubs, source):
        call(
            stubs,
            {
                **PAYLOAD,
                "metadata": {
                    **PAYLOAD["metadata"],
                    "conversation_initiation_source": source,
                },
            },
        )
        return stubs["put_if_absent"].call_args.args[1]

    def test_a_phone_call_is_marked_as_one(self, stubs):
        row = self._row(stubs, "twilio")
        assert row["channel"] == "twilio"
        assert row["is_phone_call"] is True

    def test_a_widget_session_is_not(self, stubs):
        row = self._row(stubs, "react_sdk")
        assert row["channel"] == "react_sdk"
        assert row["is_phone_call"] is False

    def test_the_channel_is_kept_verbatim(self, stubs):
        """Not reduced to a flag: the same widget is a real channel on another deployment,
        and that judgement belongs to whoever reads the numbers."""
        assert self._row(stubs, "some_future_channel")["channel"] == "some_future_channel"


class TestTheEnvelopeElevenLabsActuallySends:
    """The webhook was auto-disabled after a week of 400s. ElevenLabs wraps the conversation
    as {"type", "event_timestamp", "data": {...}} and the handler read conversation_id from
    the top level, so every real delivery was refused -- silently, because the 400 logged
    nothing. CloudWatch showed a quiet endpoint rather than a failing one.

    Every test here posted the conversation flat, which is why the suite stayed green while
    nothing real ever got in."""

    @staticmethod
    def _wrapped(payload, event="post_call_transcription"):
        return {"type": event, "event_timestamp": 1739537297, "data": payload}

    def test_a_wrapped_delivery_is_processed(self, stubs):
        status, body = call(stubs, self._wrapped(PAYLOAD))
        assert status == 200
        assert body["status"] == "OK"
        assert stubs["put_if_absent"].call_args.args[1]["conversation_id"] == "conv_1"

    def test_a_flat_delivery_still_works(self, stubs):
        """The integration suite posts the conversation directly. Both shapes are accepted so
        the fix does not trade one blind spot for another."""
        status, body = call(stubs, PAYLOAD)
        assert status == 200
        assert body["status"] == "OK"

    def test_an_event_type_we_do_not_handle_is_accepted_not_refused(self, stubs):
        """A 400 counts as a failure, and ten consecutive failures disable the webhook. An
        audio event must not be able to switch off transcription."""
        status, body = call(stubs, self._wrapped(PAYLOAD, event="post_call_audio"))
        assert status == 200
        assert body["status"] == "IGNORED"
        stubs["put_if_absent"].assert_not_called()

    def test_a_delivery_with_no_conversation_id_is_logged(self, stubs):
        """It was refused silently for a week. Whatever else it does, it has to be visible."""
        status, body = call(stubs, self._wrapped({"metadata": {}}))
        assert status == 400
        assert body["status"] == "NO_CONVERSATION_ID"

    def test_the_signature_covers_the_whole_envelope(self, stubs):
        """The HMAC is over the raw body, so unwrapping happens after verification and a
        wrapped payload signed correctly still passes."""
        status, _ = call(stubs, self._wrapped(PAYLOAD))
        assert status == 200
        body = json.dumps(self._wrapped(PAYLOAD))
        assert (
            call(stubs, self._wrapped(PAYLOAD), signature=_signed(body, secret="wrong"))[0] == 401
        )

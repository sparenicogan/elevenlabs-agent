"""US7: the nine rates in FR-043.

None is stored precomputed. A stored rate is a number that was true once and is asserted for
ever; these are functions of records that already exist, so they cannot drift from them.
"""

from src.domain.metrics import Interaction, derive, from_record

NINE = {
    "first_call_resolution",
    "autonomous_resolution_rate",
    "escalation_rate",
    "average_handling_time_seconds",
    "tool_error_rate",
    "verification_failure_rate",
    "credit_issuance_rate",
    "blocked_credit_rate",
    "containment_rate",
}


def _call(**overrides) -> Interaction:
    base = {"conversation_id": "c", "outcome": "RESOLVED_AUTONOMOUS"}
    return Interaction(**{**base, **overrides})


class TestAllNineAreDerivable:
    def test_every_rate_in_the_specification_is_produced(self):
        assert set(derive([_call()])) == NINE

    def test_none_of_them_is_stored_on_the_record(self):
        """FR-043. The record holds what happened; the rate is computed when it is asked for."""
        import inspect

        from src.handlers import post_call

        stored = inspect.getsource(post_call._store_metrics)
        for rate in NINE:
            assert rate not in stored


class TestTheyReadTheRecordsHonestly:
    def test_an_empty_period_is_zero_and_not_a_crash(self):
        """A dashboard that is simply new must not divide by zero."""
        assert derive([]) == dict.fromkeys(NINE, 0.0)

    def test_an_escalated_call_is_not_autonomous_resolution(self):
        rates = derive([_call(outcome="ESCALATED")])
        assert rates["autonomous_resolution_rate"] == 0.0
        assert rates["escalation_rate"] == 1.0

    def test_containment_is_the_complement_of_escalation(self):
        rates = derive([_call(), _call(outcome="TRANSFERRED")])
        assert rates["escalation_rate"] + rates["containment_rate"] == 1.0

    def test_the_tool_error_rate_is_per_call_not_per_conversation(self):
        """One conversation making twenty tool calls and failing once is not a 100% error
        rate."""
        rates = derive([_call(tool_calls=20, tool_failures=1)])
        assert rates["tool_error_rate"] == 0.05

    def test_verification_failures_count_only_calls_that_attempted_it(self):
        """Someone asking for opening hours never verified, and is not a verification
        failure."""
        rates = derive(
            [_call(verification_result="VERIFIED"), _call(verification_result="FAILED"), _call()]
        )
        assert rates["verification_failure_rate"] == 0.5

    def test_a_repeat_caller_is_not_a_first_call_resolution(self):
        rates = derive([_call(repeat_within_window=True)])
        assert rates["first_call_resolution"] == 0.0

    def test_the_blocked_rate_is_of_requests_not_of_calls(self):
        rates = derive([_call(credits_requested=1, credits_blocked=3)])
        assert rates["blocked_credit_rate"] == 0.75

    def test_handling_time_is_an_average_in_seconds(self):
        assert (
            derive([_call(duration_seconds=100), _call(duration_seconds=200)])[
                "average_handling_time_seconds"
            ]
            == 150.0
        )


class TestReadingAStoredRecord:
    def test_a_call_that_ended_before_anything_was_recorded_still_counts(self):
        """Missing fields become their neutral value. A call that dropped in the first second
        is a call that happened, and dropping it would flatter every rate."""
        interaction = from_record({"conversation_id": "c1"})
        assert interaction.outcome == "ABANDONED"
        assert interaction.tool_calls == 0

    def test_tool_counts_are_summed_across_every_tool(self):
        interaction = from_record(
            {
                "conversation_id": "c1",
                "tools_invoked": {
                    "verify_identity": {"count": 3, "failures": 1},
                    "match_payment": {"count": 1, "failures": 0},
                },
            }
        )
        assert interaction.tool_calls == 4
        assert interaction.tool_failures == 1

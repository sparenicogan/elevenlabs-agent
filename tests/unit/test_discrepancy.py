"""US8: the address on a payment differs from the one on file.

The rule is narrow and the failure modes are all about timing. Raised too early it tells an
unverified caller something about the account; raised as a second ticket it splits the review
in two; acted on directly it changes a record the agent has no authority over.
"""

import json

import pytest

from src.domain.handoff import compose_handoff


class TestWhenItMayBeRaised:
    def test_the_flag_is_absent_until_a_payment_matches(self):
        """FR-031. Before a MATCH there is no payment to have an address on, and saying
        anything about one would describe a record the caller has not been connected to."""
        import inspect

        from src.handlers import match_payment

        source = inspect.getsource(match_payment._body)
        early_return = source.index("return body")
        assert source.index("address_discrepancy") > early_return


class TestWhatReachesThePerson:
    def test_the_explanation_is_carried_in_the_callers_own_words(self):
        handoff = compose_handoff(
            reason="ADDRESS_DISCREPANCY",
            verified=True,
            conversation_id="conv_1",
            company_name="Alpina Tech",
            discrepancy={"field": "payer_address", "caller_explanation": "we haven't moved"},
        )
        assert "Discrepancy on payer_address" in handoff
        assert "we haven't moved" in handoff

    def test_an_unanswered_discrepancy_is_marked_unknown_not_guessed(self):
        handoff = compose_handoff(
            reason="ADDRESS_DISCREPANCY",
            verified=True,
            conversation_id="conv_1",
            discrepancy={"field": "payer_address"},
        )
        assert "UNKNOWN" in handoff

    def test_no_address_value_is_written_into_the_handoff(self):
        """FR-031b. The person reviewing it can see both addresses; the handoff is a summary
        of what the caller said, not a copy of the record."""
        handoff = compose_handoff(
            reason="ADDRESS_DISCREPANCY",
            verified=True,
            conversation_id="conv_1",
            discrepancy={"field": "payer_address", "caller_explanation": "typo"},
        )
        assert "Zollikon" not in handoff
        assert "Industriestrasse" not in handoff


class TestItJoinsTheReviewAlreadyOpen:
    def test_naming_an_existing_ticket_appends_rather_than_creating(self, mocker):
        """FR-031a. The allocation review and the address correction are one piece of work for
        one person; two tickets would be two people finding half of it."""
        from src.handlers import create_escalation as module

        mocker.patch.object(module.secrets, "get", return_value="k")
        mocker.patch.object(module.policy_module, "load", return_value=mocker.Mock())
        mocker.patch.object(
            module.conversation_state,
            "verified_context",
            return_value=("445909044455", {"company_name": "Alpina Tech"}),
        )
        mocker.patch.object(module.conversation_state, "risk_signals", return_value=[])
        mocker.patch.object(module.conversation_state, "record_callback")
        mocker.patch.object(module.audit, "write")
        append = mocker.patch.object(module.hubspot, "append_note")
        create = mocker.patch.object(module.hubspot, "create_ticket")

        response = module.handler(
            {
                "headers": {"x-api-key": "k"},
                "body": json.dumps(
                    {
                        "conversation_id": "conv_1",
                        "reason": "ADDRESS_DISCREPANCY",
                        "existing_ticket_id": "8801",
                        "discrepancy": {
                            "field": "payer_address",
                            "caller_explanation": "it's a typo",
                        },
                    }
                ),
            }
        )

        body = json.loads(response["body"])
        assert body["ticket_id"] == "8801"
        create.assert_not_called()
        assert "it's a typo" in append.call_args.args[1]


class TestTheAgentChangesNothing:
    @pytest.mark.parametrize(
        "handler", ["create_escalation", "match_payment", "propose_allocation"]
    )
    def test_no_handler_writes_an_address(self, handler):
        """The correction is a person's job. The agent records what was said and stops."""
        import importlib
        import inspect

        source = inspect.getsource(importlib.import_module(f"src.handlers.{handler}"))
        assert "payer_address =" not in source
        assert "postal_address" not in source

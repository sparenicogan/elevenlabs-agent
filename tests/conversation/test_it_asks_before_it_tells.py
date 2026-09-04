"""Ordering: the agent must not hand a caller the answer before asking for it.

The failure this exists for: the agent announced "invoice INV-2026-0013, for four thousand
two hundred francs" and *then* asked how much the caller had transferred. Since the payment
equals the invoice, it had given away the answer — an honest caller repeats it back and the
question has established nothing.

This is purely a matter of what is said in what order, so no backend check can see it.
"""

import pytest

from tests.conversation.conftest import assert_no_money_spoken

pytestmark = pytest.mark.conversation

DISPUTING_CALLER = (
    "You are Klaus Mueller from Alpina Tech. Your email is klaus.mueller@alpina-tech.ch, "
    "your phone is 044 501 22 18, your date of birth is the twelfth of March 1974, and the "
    "customer number is 445909044455. You answer identity questions correctly. You are calling "
    "because you received a reminder for an invoice you already paid. You do NOT volunteer "
    "the amount or the date of the payment unless you are asked for them directly. If asked, "
    "the amount was 4200 francs and you sent it on the 27th of July 2026."
)


class TestTheInvoiceAmountIsNotGivenAway:
    def test_no_amount_is_spoken_before_the_payment_is_matched(self, simulate, seeded):
        transcript = simulate(
            persona=DISPUTING_CALLER,
            first_message="Hello, Klaus Mueller here — a reminder for an invoice I paid.",
            turns=16,
        )
        assert "match_payment" in transcript.tools_called(), (
            f"never got as far as matching\n\n{transcript}"
        )
        assert_no_money_spoken(
            transcript,
            transcript.said_before_tool("match_payment"),
            "spoke a figure before asking what the caller paid",
        )

    def test_the_invoice_is_identified_by_number_rather_than_amount(self, simulate, seeded):
        """A caller needs to know which invoice is meant. The number and date do that
        without handing over the figure."""
        transcript = simulate(
            persona=DISPUTING_CALLER,
            first_message="Hi, Klaus Mueller here about an invoice reminder.",
            turns=16,
        )
        before = transcript.said_before_tool("match_payment").lower()
        # Spoken as well as written: an agent reading a reference aloud says "twenty
        # twenty-six" or "oh oh one three" as readily as it says INV-2026-0013.
        identified = any(
            form in before
            for form in ("inv-2026", "inv 2026", "twenty twenty-six", "0013", "one three")
        )
        assert identified, f"never told the caller which invoice was meant\n\n{transcript}"


class TestItAsksForBothValues:
    def test_it_asks_for_the_amount_and_the_date(self, simulate, seeded):
        """Matching needs both. Asking for one is a question that cannot resolve anything."""
        transcript = simulate(
            persona=DISPUTING_CALLER,
            first_message="Hello, Klaus Mueller — about an invoice I've already settled.",
            turns=16,
        )
        said = transcript.agent_said.lower()
        assert "amount" in said or "how much" in said, f"never asked the amount\n\n{transcript}"
        assert "date" in said or "when" in said, f"never asked the date\n\n{transcript}"

    def test_it_offers_to_wait_while_they_look_it_up(self, simulate, seeded):
        """Exact matching requires a lookup most callers cannot do from memory. Without the
        offer, the agent rushes them into a guess."""
        transcript = simulate(
            persona=DISPUTING_CALLER,
            first_message="Hello, this is Klaus Mueller about a payment.",
            turns=16,
        )
        said = transcript.agent_said.lower()
        offers = any(
            p in said for p in ("wait", "take your time", "no rush", "banking app", "bank")
        )
        assert offers, f"never offered to wait\n\n{transcript}"

"""US7: what is remembered about a customer between calls.

The summary exists so somebody who explained their problem last week is not asked again. It
is narrative only. Everything financial is read live, because a remembered number is a number
that can be wrong (FR-039c).
"""

from src.domain.summary import redact, regenerate

MAX = 2000


class TestItStaysWithinItsCeiling:
    def test_ten_successive_calls_do_not_grow_it_without_bound(self):
        """A summary that grows for ever stops being read, which is the same as not having
        one."""
        text = ""
        for n in range(10):
            text = regenerate(text, f"Call {n}: the caller asked about a reminder.", MAX)
            assert len(text) <= MAX

    def test_the_newest_call_survives_the_trim(self):
        """When something has to go it should be the thing longest ago."""
        old = regenerate("", "The oldest thing that happened. " * 80, MAX)
        new = regenerate(old, "Rang today about a delivery.", MAX)
        assert "Rang today about a delivery." in new
        assert len(new) <= MAX

    def test_it_never_ends_mid_sentence(self):
        long_previous = "Something happened once. " * 200
        text = regenerate(long_previous, "Rang today.", MAX)
        assert text.endswith(".") or len(text) == MAX

    def test_a_first_call_starts_from_nothing(self):
        assert regenerate("", "Asked about an invoice.", MAX) == "Asked about an invoice."

    def test_a_call_with_nothing_to_say_keeps_what_was_there(self):
        assert regenerate("Rang last week.", "", MAX) == "Rang last week."


class TestItCarriesNothingFinancial:
    def test_an_amount_never_survives(self):
        assert "4,200" not in redact("They disputed CHF 4,200.00 on the invoice.")

    def test_an_invoice_number_never_survives(self):
        assert "INV-2026-0013" not in redact("About INV-2026-0013.")

    def test_a_date_never_survives(self):
        """A date of birth is a verification answer, and no date is worth the risk of
        storing one."""
        assert "1974-03-12" not in redact("Born 1974-03-12.")

    def test_an_email_or_phone_never_survives(self):
        assert "klaus.mueller@alpina-tech.ch" not in redact(
            "Reach them at klaus.mueller@alpina-tech.ch"
        )
        assert "+41 44 501 22 18" not in redact("Call +41 44 501 22 18")

    def test_the_narrative_survives_the_redaction(self):
        """Redacting rather than rejecting: a summary that fails to save is a caller
        repeating themselves next week."""
        text = redact("Disputed an invoice for CHF 4,200.00 and was polite about it.")
        assert "Disputed an invoice" in text
        assert "polite about it" in text

    def test_a_regenerated_summary_is_safe_even_when_its_input_was_not(self):
        text = regenerate("", "They paid CHF 900.00 against INV-2026-0044 on 2026-07-01.", MAX)
        for forbidden in ("900.00", "INV-2026-0044", "2026-07-01"):
            assert forbidden not in text

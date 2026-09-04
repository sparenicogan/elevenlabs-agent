"""US6: amounts and dates as each language says them.

The agent speaks these rather than printing them, so the rule is what a person would say. A
German speaker does not say "March 14th" and a French one does not say "14 March 2026".
"""

from datetime import date
from decimal import Decimal

import pytest

from src.domain.locale import SUPPORTED, money, normalise, spoken_date


class TestAmounts:
    @pytest.mark.parametrize(
        "language,expected",
        [
            ("en", "4'200.00 francs"),
            ("de", "4'200.00 Franken"),
            ("fr", "4'200.00 francs"),
            ("it", "4'200.00 franchi"),
        ],
    )
    def test_the_currency_is_named_as_it_is_said(self, language, expected):
        assert money(Decimal("4200"), language) == expected

    def test_nothing_is_ever_rounded(
        self,
    ):
        """FR-035. Exact figures or none — an approximated amount on a billing call is worse
        than no amount."""
        assert money(Decimal("4200.456"), "en").startswith("4'200.46")
        assert money(Decimal("0.01"), "en") == "0.01 francs"

    def test_a_credit_keeps_its_sign(self):
        assert money(Decimal("-95.00"), "de").startswith("-95.00")

    @pytest.mark.parametrize("language", SUPPORTED)
    def test_every_language_groups_thousands_the_swiss_way(self, language):
        assert "1'234'567.00" in money(Decimal("1234567"), language)


class TestDates:
    @pytest.mark.parametrize(
        "language,expected",
        [
            ("en", "6 July 2026"),
            ("de", "6. Juli 2026"),
            ("fr", "le 6 juillet 2026"),
            ("it", "il 6 luglio 2026"),
        ],
    )
    def test_each_language_says_the_date_its_own_way(self, language, expected):
        assert spoken_date(date(2026, 7, 6), language) == expected

    def test_an_iso_string_is_accepted_too(self):
        assert spoken_date("2026-07-06", "de") == "6. Juli 2026"

    def test_the_month_is_named_never_numbered(self):
        """ "03/12" is two different days depending on who reads it. A named month is not."""
        for language in SUPPORTED:
            assert "03" not in spoken_date("2026-03-12", language)


class TestChoosingTheLanguage:
    def test_a_regional_code_reduces_to_its_language(self):
        assert normalise("de-CH") == "de"
        assert normalise("fr_CH") == "fr"

    def test_an_unsupported_language_falls_back_to_german(self):
        """FR-033a. The largest part of the customer base."""
        assert normalise("es") == "de"
        assert normalise("") == "de"
        assert normalise(None) == "de"

    @pytest.mark.parametrize("language", SUPPORTED)
    def test_a_supported_language_is_left_alone(self, language):
        assert normalise(language) == language

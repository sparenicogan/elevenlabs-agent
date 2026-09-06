"""The CRM read the credit ceiling depends on.

Everything else mocks hubspot at the function boundary, which left the request this builds
and the response it parses untested. Both matter now: an open credit request lives only in
HubSpot, so a wrong filter or a swallowed failure is money given away twice.
"""

import pytest

from src.adapters import hubspot
from src.adapters.errors import ErrorCategory, ToolError


def _search_body(mocker, results):
    """Captures the search request the adapter builds. Returns (payload, parsed tickets)."""
    request = mocker.patch.object(hubspot, "_request", return_value={"results": results})
    tickets = hubspot.get_pending_requests("company-77")
    return request.call_args.args[2], tickets


def test_it_asks_for_one_company_and_only_undecided_tickets(mocker):
    payload, _ = _search_body(mocker, [])

    filters = payload["filterGroups"][0]["filters"]
    assert {
        "propertyName": "associations.company",
        "operator": "EQ",
        "value": "company-77",
    } in filters
    # Absence of an outcome is what "nobody has answered this yet" means.
    assert {"propertyName": "request_outcome", "operator": "NOT_HAS_PROPERTY"} in filters


def test_it_requests_the_fields_the_ceiling_is_computed_from(mocker):
    payload, _ = _search_body(mocker, [])

    assert "credit_amount" in payload["properties"]
    assert "related_entry_id" in payload["properties"]


def test_it_reads_a_pending_credit_request(mocker):
    _, tickets = _search_body(
        mocker,
        [{"id": "8801", "properties": {"credit_amount": "80.00", "related_entry_id": "inv_1"}}],
    )

    assert tickets == [{"id": "8801", "credit_amount": "80.00", "related_entry_id": "inv_1"}]


def test_it_drops_a_property_outside_the_allowlist(mocker):
    _, tickets = _search_body(
        mocker,
        [{"id": "8801", "properties": {"credit_amount": "80.00", "date_of_birth": "1978-04-02"}}],
    )

    assert tickets == [{"id": "8801", "credit_amount": "80.00"}]


def test_one_call_serves_a_whole_company(mocker):
    """The previous implementation issued one GET per ticket, on every verified call."""
    request = mocker.patch.object(
        hubspot, "_request", return_value={"results": [{"id": str(n)} for n in range(30)]}
    )

    hubspot.get_pending_requests("company-77")

    assert request.call_count == 1


def test_a_failed_read_raises_rather_than_reporting_nothing_pending(mocker):
    """The failure that would matter: an outage read as "this company has used no credit"."""
    mocker.patch.object(
        hubspot, "_request", side_effect=ToolError(ErrorCategory.DEPENDENCY_DOWN, "hubspot down")
    )

    with pytest.raises(ToolError):
        hubspot.get_pending_requests("company-77")


class TestNotesCarryATimestamp:
    """Every note this project tried to write was refused with a 400. HubSpot requires
    hs_timestamp on /crm/v3/objects/notes and both callers sent only hs_note_body, so the
    applier's record of what it did and the per-call CRM log (FR-044) never reached HubSpot.
    The ledger writes came first and were fine, which is why it showed up as an applier that
    reported failure while the money moved correctly."""

    @staticmethod
    def _posted(mocker, call):
        module = __import__("src.adapters.hubspot", fromlist=["hubspot"])
        request = mocker.patch.object(module, "_request", return_value={"id": "n1"})
        call(module)
        return [c for c in request.call_args_list if c.args[0] == "POST"][0].args[2]

    def test_a_ticket_note_carries_one(self, mocker):
        body = self._posted(mocker, lambda m: m.append_note("T1", "applied"))
        assert body["properties"]["hs_timestamp"]
        assert body["properties"]["hs_note_body"] == "applied"

    def test_a_contact_log_carries_one(self, mocker):
        body = self._posted(mocker, lambda m: m.log_interaction("C1", "called"))
        assert body["properties"]["hs_timestamp"]

    def test_the_timestamp_is_the_format_hubspot_accepts(self, mocker):
        """ISO-8601 UTC. HubSpot rejects a bare unix epoch here."""
        import re

        body = self._posted(mocker, lambda m: m.append_note("T1", "x"))
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", body["properties"]["hs_timestamp"]
        )

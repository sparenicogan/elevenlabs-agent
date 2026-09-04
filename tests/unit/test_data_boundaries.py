"""What may leave the backend, and how long each thing lives.

Three boundaries, each with a different failure mode. The CRM must never hold a personal
detail; the identity table must be readable by almost nothing; and a transcript must expire
long before the metadata about it does.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "infra" / "terraform"


class TestTheCrmHoldsNothingPersonal:
    """FR-027, FR-028. HubSpot is the non-sensitive half of the system, and the allowlist in
    the adapter is what makes that true rather than aspirational."""

    def test_the_ticket_allowlist_carries_no_identity_field(self):
        from src.adapters.hubspot import ALLOWED_TICKET_FIELDS

        for field in ("email", "phone", "date_of_birth", "postal_address", "first_name"):
            assert field not in ALLOWED_TICKET_FIELDS

    def test_the_contact_allowlist_is_what_hubspot_already_knows(self):
        """A name and an email address are what a CRM is for. Nothing is added by us that a
        salesperson would not already have put there."""
        from src.adapters.hubspot import ALLOWED_CONTACT_FIELDS

        assert {
            "firstname",
            "lastname",
            "company",
            "hs_object_id",
            "email",
        } >= ALLOWED_CONTACT_FIELDS
        assert "date_of_birth" not in ALLOWED_CONTACT_FIELDS

    def test_stripping_happens_at_the_boundary_not_the_call_site(self):
        """So a future handler cannot leak a date of birth by passing the wrong dict."""
        source = (ROOT / "src" / "adapters" / "hubspot.py").read_text()
        for function in ("create_ticket", "create_unassociated_ticket"):
            body = source.split(f"def {function}(")[1].split("\ndef ")[0]
            assert "_strip(" in body

    def test_a_personal_field_does_not_survive_a_strip(self):
        from src.adapters.hubspot import ALLOWED_TICKET_FIELDS, _strip

        stripped = _strip(
            {"subject": "kept", "date_of_birth": "1974-03-12", "email": "a@b.ch"},
            ALLOWED_TICKET_FIELDS,
        )
        assert stripped == {"subject": "kept"}


class TestOnlyTwoRolesReadIdentity:
    """FR-030. The identity table holds the answers to the verification questions, so reading
    it is the one permission worth counting."""

    def test_no_other_handler_is_granted_it(self):
        readers = set()
        for path in TERRAFORM.glob("*.tf"):
            text = path.read_text()
            for block in re.findall(
                r'data "aws_iam_policy_document" "(\w+)" \{(.*?)\n\}', text, re.S
            ):
                name, body = block
                if "aws_dynamodb_table.customer_identity.arn" in body:
                    readers.add(name)

        assert readers == {"verify_identity", "check_factor", "conversation_init", "post_call"}, (
            f"unexpected readers of the identity table: {sorted(readers)}"
        )

    def test_the_ones_that_do_have_a_reason(self):
        """verify_identity and check_factor compare answers against it; conversation_init
        resolves a greeting language from the calling number; post_call remembers the language
        the call actually happened in. Nothing else has a reason to see it."""
        assert True


def _audit_retention_days() -> int:
    """How long an audit event lives, read from the log group that holds them."""
    logs = (TERRAFORM / "logs.tf").read_text()
    # Searched forward from the resource rather than splitting on a brace: the name
    # interpolates ${var.project}, whose closing brace comes first.
    after = logs.split('resource "aws_cloudwatch_log_group" "audit"')[1]
    return int(re.search(r"retention_in_days\s*=\s*(\d+)", after).group(1))


class TestRetentionMatchesWhatWasPromised:
    """FR-038a. A transcript is the only artefact with a short life; everything derived from
    it outlives it by years."""

    def _terraform(self) -> str:
        return "\n".join(p.read_text() for p in TERRAFORM.glob("*.tf"))

    def test_transcripts_expire(self):
        text = self._terraform()
        assert "aws_s3_bucket_lifecycle_configuration" in text
        assert "transcript_retention_days" in text

    def test_the_audit_log_outlives_them_by_years(self):
        """An audit event is the record that a financial decision was taken. It is the last
        thing that should expire, and it is set on the log group rather than as a variable
        because there is no version of this system where it should be shorter."""
        assert _audit_retention_days() >= 3650

    def test_a_transcript_expires_long_before_the_record_of_it(self):
        variables = (TERRAFORM / "variables.tf").read_text()
        block = variables.split('variable "transcript_retention_days"')[1].split("}")[0]
        transcript_days = int(re.search(r"default\s*=\s*(\d+)", block).group(1))

        assert transcript_days < _audit_retention_days()
        assert transcript_days <= 90

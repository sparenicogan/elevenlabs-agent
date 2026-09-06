# Four tables, separated by sensitivity and retention class rather than by convenience.
# See specs/001-voice-billing-agent/data-model.md.

# One row per person, not per company. A company's account may have several contacts, and
# each verifies with their own email, phone and date of birth — which is what separates a
# listed contact of a customer from somebody who merely knows about that customer.
#
# account_id points at the company, and every financial record is keyed by that. So a person
# authenticates and a company account is what they reach.
resource "aws_dynamodb_table" "customer_identity" {
  name         = "${var.project}-customer-identity"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "contact_id"

  attribute {
    name = "contact_id"
    type = "S"
  }

  attribute {
    name = "account_id"
    type = "S"
  }

  attribute {
    name = "phone_lookup"
    type = "S"
  }

  attribute {
    name = "email_lookup"
    type = "S"
  }

  # Serves the greeting-language lookup in the conversation initiation webhook.
  # Projects only the two fields that lookup needs, so a compromised query cannot
  # return identity data (FR-033b).
  global_secondary_index {
    name            = "phone-index"
    hash_key        = "phone_lookup"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "customer_id",
      "preferred_language",
    ]
  }

  # Lets verification find the person from an email address. Without it a caller must recite
  # their customer id before any other answer can be checked at all, and every correct
  # answer given first is scored as wrong.
  global_secondary_index {
    name            = "email-index"
    hash_key        = "email_lookup"
    projection_type = "KEYS_ONLY"
  }

  # Every contact belonging to one company account. Used to check that a caller naming a
  # customer id is actually one of that account's people.
  global_secondary_index {
    name            = "account-index"
    hash_key        = "account_id"
    projection_type = "KEYS_ONLY"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.identity.arn
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_dynamodb_table" "ledger" {
  name         = "${var.project}-ledger"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "customer_id"
  range_key    = "entry_id"

  attribute {
    name = "customer_id"
    type = "S"
  }

  attribute {
    name = "entry_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "payment_reference"
    type = "S"
  }

  attribute {
    name = "invoice_number"
    type = "S"
  }

  # Open invoices and unallocated payments are read by status on every call.
  # Without this index those reads become table scans.
  global_secondary_index {
    name            = "status-index"
    hash_key        = "customer_id"
    range_key       = "status"
    projection_type = "ALL"
  }

  # Answers one question: does this string resolve to an existing invoice? That is what
  # separates a mistyped reference from a payment earmarked for a different invoice
  # (FR-010g), and it must span customers — a payment quoting another customer's reference
  # belongs to them, not to the caller on the line.
  global_secondary_index {
    name            = "reference-index"
    hash_key        = "payment_reference"
    projection_type = "KEYS_ONLY"
  }

  # The same question asked of the other identifier. A payment may quote the invoice number
  # rather than the payment reference, and if that number belongs to a different invoice the
  # money is earmarked there. Without this index that case is unresolvable and the safe
  # answer would have to be a refusal, which would strand legitimate payments.
  global_secondary_index {
    name            = "invoice-number-index"
    hash_key        = "invoice_number"
    projection_type = "KEYS_ONLY"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.data.arn
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_dynamodb_table" "conversations" {
  name         = "${var.project}-conversations"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "conversation_id"

  attribute {
    name = "conversation_id"
    type = "S"
  }

  attribute {
    name = "customer_id"
    type = "S"
  }

  attribute {
    name = "started_at"
    type = "S"
  }

  # Risk-signal aggregation reads a customer's recent conversations in time order.
  global_secondary_index {
    name            = "customer-index"
    hash_key        = "customer_id"
    range_key       = "started_at"
    projection_type = "ALL"
  }

  # Live call state expires on its own. Verification outcomes, lockout counters and the
  # fingerprints of what a caller guessed are useful for minutes and a liability for months.
  # What survives a call is written to the interactions table instead.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.data.arn
  }
}

# One immutable row per completed call. Separate from conversations because the two have
# opposite lifetimes: call state is worthless an hour later, and what a call cost, how long
# the caller waited and whether it resolved is worth years. Sharing a row meant the only
# available reset destroyed both.
resource "aws_dynamodb_table" "interactions" {
  name         = "${var.project}-interactions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "conversation_id"

  attribute {
    name = "conversation_id"
    type = "S"
  }

  attribute {
    name = "customer_id"
    type = "S"
  }

  attribute {
    name = "started_at"
    type = "S"
  }

  # Two readers: the credit risk rules ask for one customer's last year of calls, and the
  # metrics script scans a date range across everybody.
  global_secondary_index {
    name            = "customer-index"
    hash_key        = "customer_id"
    range_key       = "started_at"
    projection_type = "ALL"
  }

  # No TTL. This table is the record of what happened, and it is never reset by seeding.
  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.data.arn
  }
}

resource "aws_dynamodb_table" "customer_summaries" {
  name         = "${var.project}-customer-summaries"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "customer_id"

  attribute {
    name = "customer_id"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.data.arn
  }
}

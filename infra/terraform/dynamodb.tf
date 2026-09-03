# Four tables, separated by sensitivity and retention class rather than by convenience.
# See specs/001-voice-billing-agent/data-model.md.

resource "aws_dynamodb_table" "customer_identity" {
  name         = "${var.project}-customer-identity"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "customer_id"

  attribute {
    name = "customer_id"
    type = "S"
  }

  attribute {
    name = "phone"
    type = "S"
  }

  # Serves the greeting-language lookup in the conversation initiation webhook.
  # Projects only the two fields that lookup needs, so a compromised query cannot
  # return identity data (FR-033b).
  global_secondary_index {
    name            = "phone-index"
    hash_key        = "phone"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "customer_id",
      "preferred_language",
    ]
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

# Transcripts are the most sensitive artefact and the shortest-lived: raw speech about
# invoices, addresses and payment details, useful for a few weeks and a liability after.

resource "aws_s3_bucket" "transcripts" {
  bucket = "${var.project}-transcripts-${var.aws_account_id}"
}

resource "aws_s3_bucket_public_access_block" "transcripts" {
  bucket                  = aws_s3_bucket.transcripts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "transcripts" {
  bucket = aws_s3_bucket.transcripts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "transcripts" {
  bucket = aws_s3_bucket.transcripts.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Expiry is enforced by the bucket, not by application code remembering to delete (FR-038a).
resource "aws_s3_bucket_lifecycle_configuration" "transcripts" {
  bucket     = aws_s3_bucket.transcripts.id
  depends_on = [aws_s3_bucket_versioning.transcripts]

  rule {
    id     = "expire-transcripts"
    status = "Enabled"

    filter {
      prefix = "customers/"
    }

    expiration {
      days = var.transcript_retention_days
    }

    # Versioning is on for recovery, so non-current versions need their own expiry
    # or deleted transcripts would outlive the retention period.
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }
}

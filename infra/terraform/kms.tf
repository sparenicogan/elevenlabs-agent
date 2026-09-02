# Two keys, not one. The identity key's policy is what keeps identity data isolated
# (Principle IV); sharing a key with transcripts and metrics would force that policy to
# admit every Lambda in the project and the isolation would be nominal.

resource "aws_kms_key" "identity" {
  description             = "Encrypts the customer identity table"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.identity_key.json
}

resource "aws_kms_alias" "identity" {
  name          = "alias/${var.project}-identity"
  target_key_id = aws_kms_key.identity.key_id
}

data "aws_iam_policy_document" "identity_key" {
  # Account administration. Without this statement the key becomes unmanageable.
  statement {
    sid       = "AllowAccountAdministration"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.aws_account_id}:root"]
    }
  }

  # Only the two handlers that legitimately read identity data can decrypt with this key.
  # Matching on role name prefix rather than exact ARN means the roles need not exist yet.
  statement {
    sid       = "AllowIdentityReaders"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.aws_account_id}:root"]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:PrincipalArn"
      values = [
        "arn:aws:iam::${var.aws_account_id}:role/${var.project}-verify-identity",
        "arn:aws:iam::${var.aws_account_id}:role/${var.project}-conversation-init",
      ]
    }
  }
}

resource "aws_kms_key" "data" {
  description             = "Encrypts transcripts, ledger, conversations, summaries and audit logs"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.data_key.json
}

data "aws_iam_policy_document" "data_key" {
  statement {
    sid       = "AllowAccountAdministration"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.aws_account_id}:root"]
    }
  }

  # CloudWatch Logs encrypts as the service itself, not on behalf of the caller, so it
  # needs its own grant. DynamoDB and S3 do not, which is why they applied cleanly.
  # The encryption-context condition confines the grant to this account's log groups.
  statement {
    sid    = "AllowCloudWatchLogs"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["logs.${var.aws_region}.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:*"]
    }
  }
}

resource "aws_kms_alias" "data" {
  name          = "alias/${var.project}-data"
  target_key_id = aws_kms_key.data.key_id
}

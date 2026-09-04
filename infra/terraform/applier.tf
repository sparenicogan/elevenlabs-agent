# The applier. The only principal in the account permitted to write the financial ledger, and
# deliberately the only Lambda with no API Gateway route: it is reachable from a schedule
# inside AWS and from nowhere on the internet. A webhook would have put the ledger-writing
# path behind an inbound HTTP request, which is the blast radius this design exists to avoid.

data "aws_iam_policy_document" "apply_decisions" {
  statement {
    effect = "Allow"
    # Writes credit notes and reads the history it re-checks them against. No DeleteItem:
    # a credit is reversed by a compensating entry, never by removing the record.
    actions   = ["dynamodb:PutItem", "dynamodb:Query", "dynamodb:GetItem"]
    resources = [aws_dynamodb_table.ledger.arn, "${aws_dynamodb_table.ledger.arn}/index/*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.data.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.hubspot_token.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter/${var.project}/policy"]
  }

  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.audit.arn}:*"]
  }
}

module "apply_decisions" {
  source = "./modules/lambda"

  name         = "apply-decisions"
  project      = var.project
  handler      = "src.handlers.apply_decisions.handler"
  package_path = local.lambda_package
  environment  = local.common_environment
  policy_json  = data.aws_iam_policy_document.apply_decisions.json
}

# A minute is well inside what this workflow notices, and it is what makes polling an
# adequate substitute for a webhook rather than a compromise.
resource "aws_cloudwatch_event_rule" "apply_decisions" {
  name                = "${var.project}-apply-decisions"
  description         = "Applies accepted credit requests to the ledger"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "apply_decisions" {
  rule = aws_cloudwatch_event_rule.apply_decisions.name
  arn  = module.apply_decisions.function_arn
}

resource "aws_lambda_permission" "apply_decisions" {
  statement_id  = "AllowScheduledInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.apply_decisions.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.apply_decisions.arn
}

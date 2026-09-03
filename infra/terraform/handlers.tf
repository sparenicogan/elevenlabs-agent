# Tool endpoints. Each handler gets its own Lambda, its own role, and its own route, so
# read access to the identity table can be granted to two functions and denied to the rest
# (Principle X).

locals {
  lambda_package = "${path.module}/../../dist/lambda.zip"

  common_environment = {
    PROJECT            = var.project
    TRANSCRIPTS_BUCKET = aws_s3_bucket.transcripts.id
  }
}

# --- verify_identity ---------------------------------------------------------------
# One of only two functions permitted to read the identity table and use its key.

data "aws_iam_policy_document" "verify_identity" {
  statement {
    effect  = "Allow"
    actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [
      aws_dynamodb_table.customer_identity.arn,
      aws_dynamodb_table.conversations.arn,
    ]
  }

  statement {
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.identity.arn, aws_kms_key.data.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.tool_api_key.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter/${var.project}/policy"]
  }
}

module "verify_identity" {
  source = "./modules/lambda"

  name         = "verify-identity"
  project      = var.project
  handler      = "src.handlers.verify_identity.handler"
  package_path = local.lambda_package
  environment  = local.common_environment
  policy_json  = data.aws_iam_policy_document.verify_identity.json
}

resource "aws_apigatewayv2_integration" "verify_identity" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.verify_identity.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 5000
}

resource "aws_apigatewayv2_route" "verify_identity" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /tools/verify-identity"
  target    = "integrations/${aws_apigatewayv2_integration.verify_identity.id}"
}

resource "aws_lambda_permission" "verify_identity" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.verify_identity.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

# --- get_account_context -----------------------------------------------------------
# Reads the ledger and the identity record, but only after the conversation is verified.
# No write permission anywhere: this handler answers questions, it does not change anything.

data "aws_iam_policy_document" "get_account_context" {
  statement {
    effect  = "Allow"
    actions = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [
      aws_dynamodb_table.ledger.arn,
      "${aws_dynamodb_table.ledger.arn}/index/*",
      aws_dynamodb_table.conversations.arn,
      aws_dynamodb_table.customer_summaries.arn,
    ]
  }

  # Deliberately excludes the identity table's key. This handler reads the identity record
  # for a company name and the HubSpot ids, which live under the data key; it has no reason
  # to decrypt a date of birth.
  statement {
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.data.arn]
  }

  statement {
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.tool_api_key.arn,
      aws_secretsmanager_secret.hubspot_token.arn,
    ]
  }

  statement {
    effect    = "Allow"
    actions   = ["ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter/${var.project}/policy"]
  }
}

module "get_account_context" {
  source = "./modules/lambda"

  name         = "get-account-context"
  project      = var.project
  handler      = "src.handlers.get_account_context.handler"
  package_path = local.lambda_package
  environment  = local.common_environment
  policy_json  = data.aws_iam_policy_document.get_account_context.json
}

resource "aws_apigatewayv2_integration" "get_account_context" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.get_account_context.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 5000
}

resource "aws_apigatewayv2_route" "get_account_context" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /tools/get-account-context"
  target    = "integrations/${aws_apigatewayv2_integration.get_account_context.id}"
}

resource "aws_lambda_permission" "get_account_context" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.get_account_context.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

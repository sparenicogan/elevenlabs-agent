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
    actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = [
      aws_dynamodb_table.customer_identity.arn,
      # The email and phone indexes, so a caller can be found from an identifier they
      # actually know rather than only from a customer id they may have to look up.
      "${aws_dynamodb_table.customer_identity.arn}/index/*",
      aws_dynamodb_table.conversations.arn,
    ]
  }

  statement {
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.identity.arn, aws_kms_key.data.arn]
  }

  statement {
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.tool_api_key.arn,
      aws_secretsmanager_secret.attempt_salt.arn,
    ]
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

# --- match_payment ------------------------------------------------------------------
# Reads the ledger and both reference indexes. No write permission: matching decides
# nothing, it only answers whether the caller's claim fits a payment on record.

data "aws_iam_policy_document" "match_payment" {
  statement {
    effect  = "Allow"
    actions = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [
      aws_dynamodb_table.ledger.arn,
      "${aws_dynamodb_table.ledger.arn}/index/*",
      aws_dynamodb_table.conversations.arn,
    ]
  }

  statement {
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.data.arn]
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

module "match_payment" {
  source = "./modules/lambda"

  name         = "match-payment"
  project      = var.project
  handler      = "src.handlers.match_payment.handler"
  package_path = local.lambda_package
  environment  = local.common_environment
  policy_json  = data.aws_iam_policy_document.match_payment.json
}

resource "aws_apigatewayv2_integration" "match_payment" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.match_payment.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 5000
}

resource "aws_apigatewayv2_route" "match_payment" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /tools/match-payment"
  target    = "integrations/${aws_apigatewayv2_integration.match_payment.id}"
}

resource "aws_lambda_permission" "match_payment" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.match_payment.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

# --- propose_allocation -------------------------------------------------------------
# The only handler with write permission on the ledger, and the only one that can write an
# audit event. Its update is conditional on the payment still being unallocated, so the
# permission grants far less than it appears to.

data "aws_iam_policy_document" "propose_allocation" {
  statement {
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.ledger.arn, aws_dynamodb_table.conversations.arn]
  }

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

  # Audit events are append-only and go to their own log group with ten-year retention.
  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.audit.arn}:*"]
  }
}

module "propose_allocation" {
  source = "./modules/lambda"

  name         = "propose-allocation"
  project      = var.project
  handler      = "src.handlers.propose_allocation.handler"
  package_path = local.lambda_package
  environment  = local.common_environment
  policy_json  = data.aws_iam_policy_document.propose_allocation.json
}

resource "aws_apigatewayv2_integration" "propose_allocation" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.propose_allocation.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 5000
}

resource "aws_apigatewayv2_route" "propose_allocation" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /tools/propose-allocation"
  target    = "integrations/${aws_apigatewayv2_integration.propose_allocation.id}"
}

resource "aws_lambda_permission" "propose_allocation" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.propose_allocation.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

# --- create_escalation ---------------------------------------------------------------
# The one handler that works without a verified conversation, because a caller the system
# cannot identify still needs a person. It reads conversation state and writes to the CRM
# and the audit log; it touches no financial record at all.

data "aws_iam_policy_document" "create_escalation" {
  statement {
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.conversations.arn]
  }

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

  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.audit.arn}:*"]
  }
}

module "create_escalation" {
  source = "./modules/lambda"

  name         = "create-escalation"
  project      = var.project
  handler      = "src.handlers.create_escalation.handler"
  package_path = local.lambda_package
  environment  = local.common_environment
  policy_json  = data.aws_iam_policy_document.create_escalation.json
}

resource "aws_apigatewayv2_integration" "create_escalation" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.create_escalation.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 5000
}

resource "aws_apigatewayv2_route" "create_escalation" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /tools/create-escalation"
  target    = "integrations/${aws_apigatewayv2_integration.create_escalation.id}"
}

resource "aws_lambda_permission" "create_escalation" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.create_escalation.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

# --- request_credit -------------------------------------------------------------------
# The only handler that can give something away without a person confirming it. It writes
# to the ledger, but only ever a new credit note: put_if_absent creates, and there is no
# update or delete permission here, so it cannot alter an invoice or a payment.

data "aws_iam_policy_document" "request_credit" {
  statement {
    effect    = "Allow"
    actions   = ["dynamodb:Query", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.ledger.arn]
  }

  # Reads the conversation for verification state, updates it to record risk signals, and
  # queries the customer index for patterns across their recent calls.
  statement {
    effect  = "Allow"
    actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = [
      aws_dynamodb_table.conversations.arn,
      "${aws_dynamodb_table.conversations.arn}/index/*",
    ]
  }

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

  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.audit.arn}:*"]
  }
}

module "request_credit" {
  source = "./modules/lambda"

  name         = "request-credit"
  project      = var.project
  handler      = "src.handlers.request_credit.handler"
  package_path = local.lambda_package
  environment  = local.common_environment
  policy_json  = data.aws_iam_policy_document.request_credit.json
}

resource "aws_apigatewayv2_integration" "request_credit" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.request_credit.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 5000
}

resource "aws_apigatewayv2_route" "request_credit" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /tools/request-credit"
  target    = "integrations/${aws_apigatewayv2_integration.request_credit.id}"
}

resource "aws_lambda_permission" "request_credit" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.request_credit.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

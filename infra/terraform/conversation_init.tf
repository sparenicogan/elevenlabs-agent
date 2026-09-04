# The conversation initiation webhook. Reads one number and answers with a language, before
# anyone has spoken. Unauthenticated by design: it discloses nothing, and a call that cannot
# be greeted is a call that is lost.

data "aws_iam_policy_document" "conversation_init" {
  source_policy_documents = [data.aws_iam_policy_document.deny_ledger_writes.json]

  statement {
    effect  = "Allow"
    actions = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [
      aws_dynamodb_table.customer_identity.arn,
      "${aws_dynamodb_table.customer_identity.arn}/index/*",
    ]
  }

  statement {
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.identity.arn]
  }
}

module "conversation_init" {
  source = "./modules/lambda"

  name         = "conversation-init"
  project      = var.project
  handler      = "src.handlers.conversation_init.handler"
  package_path = local.lambda_package
  environment  = local.common_environment
  policy_json  = data.aws_iam_policy_document.conversation_init.json
}

resource "aws_apigatewayv2_integration" "conversation_init" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.conversation_init.invoke_arn
  payload_format_version = "2.0"
  # Short: a caller is listening to silence until this answers.
  timeout_milliseconds = 3000
}

resource "aws_apigatewayv2_route" "conversation_init" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /webhooks/conversation-initiation"
  target    = "integrations/${aws_apigatewayv2_integration.conversation_init.id}"
}

resource "aws_lambda_permission" "conversation_init" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.conversation_init.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

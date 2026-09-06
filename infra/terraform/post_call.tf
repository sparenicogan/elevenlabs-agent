# The post-call webhook. Signed by ElevenLabs and verified here, so it is the one inbound
# route that is not protected by the tool API key.

data "aws_iam_policy_document" "post_call" {
  source_policy_documents = [data.aws_iam_policy_document.deny_ledger_writes.json]

  statement {
    effect  = "Allow"
    actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [
      aws_dynamodb_table.conversations.arn,
      aws_dynamodb_table.customer_summaries.arn,
      # The language a call actually happened in, remembered for the next one.
      aws_dynamodb_table.customer_identity.arn,
    ]
  }

  # The permanent record. PutItem only: the row is written once and never updated, and the
  # handler has no reason to be able to change one after the fact.
  statement {
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.performance.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.transcripts.arn}/*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.data.arn, aws_kms_key.identity.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.elevenlabs_webhook.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter/${var.project}/policy"]
  }
}

module "post_call" {
  source = "./modules/lambda"

  name         = "post-call"
  project      = var.project
  handler      = "src.handlers.post_call.handler"
  package_path = local.lambda_package
  environment  = local.common_environment
  policy_json  = data.aws_iam_policy_document.post_call.json
}

resource "aws_apigatewayv2_integration" "post_call" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.post_call.invoke_arn
  payload_format_version = "2.0"
  # Longer than a tool call: nobody is waiting on the line for this one.
  timeout_milliseconds = 20000
}

resource "aws_apigatewayv2_route" "post_call" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /webhooks/post-call"
  target    = "integrations/${aws_apigatewayv2_integration.post_call.id}"
}

resource "aws_lambda_permission" "post_call" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.post_call.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

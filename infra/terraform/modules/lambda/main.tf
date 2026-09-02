# One Lambda plus its own role and log group. Every handler gets a distinct role so that
# read access to the identity table can be granted to two functions and denied to the
# rest (Principle X, T097).

resource "aws_iam_role" "this" {
  name = "${var.project}-${var.name}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "this" {
  name   = "${var.project}-${var.name}"
  role   = aws_iam_role.this.id
  policy = var.policy_json
}

# Written explicitly rather than left to Lambda's implicit creation, so retention is set
# and the group is destroyed with the function.
resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.project}-${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role_policy" "logs" {
  name = "${var.project}-${var.name}-logs"
  role = aws_iam_role.this.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${aws_cloudwatch_log_group.this.arn}:*"
    }]
  })
}

resource "aws_lambda_function" "this" {
  function_name    = "${var.project}-${var.name}"
  role             = aws_iam_role.this.arn
  handler          = var.handler
  runtime          = "python3.12"
  timeout          = var.timeout_seconds
  memory_size      = 512
  filename         = var.package_path
  source_code_hash = filebase64sha256(var.package_path)

  environment {
    variables = var.environment
  }

  depends_on = [aws_cloudwatch_log_group.this]
}

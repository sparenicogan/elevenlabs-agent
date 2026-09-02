# Audit events live here rather than in DynamoDB: they are append-only and never read by
# key during a call, so a log group with retention and Insights covers the review case
# without a table (plan.md, Complexity Tracking).

resource "aws_cloudwatch_log_group" "audit" {
  name              = "/${var.project}/audit"
  retention_in_days = 3653 # 10 years, matching the financial record-keeping period (FR-038a)
  kms_key_id        = aws_kms_key.data.arn
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/${var.project}/api-access"
  retention_in_days = 90
}

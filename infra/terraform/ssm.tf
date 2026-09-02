# Policy, not code. Changing a threshold is a parameter edit that takes effect within one
# Lambda lifecycle — no prompt edit, no redeploy (Principle VIII, FR-045).

locals {
  policy_parameters = {
    required_factor_count     = "3"
    verification_max_attempts = "3"
    credit_max_per_request    = "100"
    credit_max_rolling        = "500"
    credit_window_months      = "12"
    allocation_authority_max  = "0"
    tool_timeout_seconds      = "5"
    read_retry_count          = "1"
    transcript_retention_days = tostring(var.transcript_retention_days)
    summary_max_chars         = "2000"
    resolution_target_hours   = "24"
  }
}

resource "aws_ssm_parameter" "policy" {
  for_each = local.policy_parameters

  name  = "/${var.project}/policy/${each.key}"
  type  = "String"
  value = each.value

  # These are operational settings a human may tune in the console; Terraform should not
  # revert a deliberate change on the next apply.
  lifecycle {
    ignore_changes = [value]
  }
}

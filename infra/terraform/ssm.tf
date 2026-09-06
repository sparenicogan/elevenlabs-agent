# Policy, not code. Changing a threshold is a parameter edit that takes effect within one
# Lambda lifecycle — no prompt edit, no redeploy (Principle VIII, FR-045).

locals {
  policy_parameters = {
    required_factor_count     = "3"
    verification_max_attempts = "3"

    # Distinct values a caller may offer for one field before it reads as enumeration
    # rather than correction. Two allows a single correction (FR-006a).
    guessing_max_distinct_values = "2"

    # Backward-only tolerance in calendar days: the payer sees the date their transfer left,
    # the record may hold the date it arrived. A Friday transfer posting on Monday is 3 days.
    payment_date_tolerance_days = "3"

    # Edit distance within which a payment reference counts as a mistyped invoice id —
    # but only when the written reference resolves to no invoice at all (FR-010g).
    reference_typo_max_distance = "2"

    # Greeting language when the caller's number is not recognised (FR-033a). German
    # for a Swiss customer base; set to en while rehearsing, since a demo you cannot
    # follow is not a demo you can debug.
    default_language = "en"

    credit_max_per_request    = "100"
    credit_max_rolling        = "500"
    credit_window_months      = "12"
    allocation_authority_max  = "0"
    tool_timeout_seconds      = "5"
    read_retry_count          = "1"
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

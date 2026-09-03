# Created empty. Values are supplied out of band with `aws secretsmanager put-secret-value`
# and never appear in the repository, in state, or in a plan output (Principle X).

resource "aws_secretsmanager_secret" "elevenlabs_webhook" {
  name        = "${var.project}/elevenlabs/webhook-secret"
  description = "HMAC shared secret used to validate the post-call webhook signature"
}

resource "aws_secretsmanager_secret" "tool_api_key" {
  name        = "${var.project}/tools/api-key"
  description = "X-Api-Key the agent sends to every tool endpoint"
}

resource "aws_secretsmanager_secret" "hubspot_token" {
  name        = "${var.project}/hubspot/private-app-token"
  description = "HubSpot private app token for contacts, tickets and engagements"
}


# Keys the fingerprints of attempted verification answers. Generated here rather than
# supplied out of band because there is nothing for a human to know: it exists only so that
# a table dump cannot be brute-forced back into the values a caller offered, and a date of
# birth is a small enough space to enumerate without one.
#
# The tradeoff, stated plainly: this value lands in Terraform state. State is in an
# encrypted, access-controlled bucket, and the salt grants no access to anything — losing it
# weakens the fingerprints, it does not open a door. A manually-managed secret would avoid
# state entirely at the cost of a step that gets skipped.
resource "random_password" "attempt_salt" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "attempt_salt" {
  name        = "${var.project}/verification/attempt-salt"
  description = "HMAC key for fingerprinting attempted verification answers"
}

resource "aws_secretsmanager_secret_version" "attempt_salt" {
  secret_id     = aws_secretsmanager_secret.attempt_salt.id
  secret_string = random_password.attempt_salt.result
}

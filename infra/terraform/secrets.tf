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

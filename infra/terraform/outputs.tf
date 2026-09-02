output "deploy_role_arn" {
  description = "Role GitHub Actions assumes via OIDC. Set as AWS_DEPLOY_ROLE_ARN in repository variables."
  value       = aws_iam_role.github_deploy.arn
}

output "api_endpoint" {
  description = "Base URL for tool endpoints and webhooks. Configure this in the ElevenLabs agent."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "transcripts_bucket" {
  description = "Where post-call transcripts are written."
  value       = aws_s3_bucket.transcripts.id
}

output "deploy_role_arn" {
  description = "Role GitHub Actions assumes via OIDC. Set as AWS_DEPLOY_ROLE_ARN in repository variables."
  value       = aws_iam_role.github_deploy.arn
}

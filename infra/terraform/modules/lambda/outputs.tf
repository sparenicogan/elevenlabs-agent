output "function_arn" {
  description = "For the API Gateway integration."
  value       = aws_lambda_function.this.arn
}

output "invoke_arn" {
  description = "For the API Gateway integration URI."
  value       = aws_lambda_function.this.invoke_arn
}

output "function_name" {
  description = "For the API Gateway invoke permission."
  value       = aws_lambda_function.this.function_name
}

output "role_name" {
  description = "For attaching additional policies from the root module."
  value       = aws_iam_role.this.name
}

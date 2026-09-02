variable "name" {
  description = "Handler name, e.g. verify-identity. Becomes the function and role name suffix."
  type        = string
}

variable "project" {
  description = "Project prefix, so every resource is sweepable by name."
  type        = string
}

variable "handler" {
  description = "Python entry point, e.g. src.handlers.verify_identity.handler"
  type        = string
}

variable "package_path" {
  description = "Path to the built deployment zip."
  type        = string
}

variable "environment" {
  description = "Environment variables for the function. Never secrets — those are read from Secrets Manager at runtime."
  type        = map(string)
  default     = {}
}

variable "policy_json" {
  description = "Least-privilege inline policy for this function. Each handler gets only the tables and keys it needs."
  type        = string
}

variable "timeout_seconds" {
  description = "Must stay below the agent's 5-second tool budget so the backend fails first and can return a speakable error (research D5)."
  type        = number
  default     = 4
}

variable "log_retention_days" {
  description = "Function log retention. Conversation metadata lives in DynamoDB; these are operational logs."
  type        = number
  default     = 90
}

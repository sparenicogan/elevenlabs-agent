variable "aws_account_id" {
  description = "Target account. Guards against applying into the wrong account; value lives in the untracked terraform.tfvars."
  type        = string
}

variable "aws_region" {
  description = "Deployment region. eu-central-1 for EU data residency (research D7)."
  type        = string
  default     = "eu-central-1"
}

variable "project" {
  description = "Name prefix for every resource, so the account can be swept by prefix."
  type        = string
  default     = "voice-agent"
}

variable "github_repo" {
  description = "owner/name of the repository allowed to assume the deploy role."
  type        = string
  default     = "sparenicogan/elevenlabs-agent"
}

variable "create_oidc_provider" {
  description = "False when the account already has a GitHub OIDC provider; an account may only have one."
  type        = bool
  default     = true
}

variable "transcript_retention_days" {
  description = "Transcripts expire after this many days (FR-038a). The shortest-lived artefact."
  type        = number
  default     = 90
}

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

# This organisation has immutable OIDC subject claims enabled, so GitHub presents
#   repo:<owner>@<owner_id>/<repo>@<repo_id>:ref:refs/heads/main
# rather than the documented repo:<owner>/<repo>:ref:... . The numeric ids survive a
# rename, which is the point: renaming a repository cannot transfer its AWS trust to
# whoever claims the freed name. A trust policy written against the documented form
# silently matches nothing.
# These two ids are public: any client can read them from the GitHub API for any public
# repository. They are kept in the repository deliberately, because the trust policy is
# meaningless without them and hiding them would protect nothing.
variable "github_owner_id" {
  description = "Numeric GitHub id of the repository owner, from the OIDC sub claim."
  type        = string
  default     = "299617649"
}

variable "github_repository_id" {
  description = "Numeric GitHub id of the repository, from the OIDC sub claim."
  type        = string
  default     = "1354831999"
}

variable "create_oidc_provider" {
  description = "False when the account already has a GitHub OIDC provider; an account may only have one."
  type        = bool
  default     = true
}


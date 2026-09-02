provider "aws" {
  region = var.aws_region

  # An apply pointed at any other account fails outright rather than quietly
  # creating resources somewhere it should not.
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}

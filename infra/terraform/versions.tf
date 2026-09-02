terraform {
  # 1.10 is the floor because it introduced S3-native state locking (use_lockfile),
  # which removes the need for a DynamoDB table that exists only to hold Terraform locks.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

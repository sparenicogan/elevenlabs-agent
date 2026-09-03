terraform {
  # Partial configuration. The bucket name is supplied at init time rather than committed,
  # because it embeds the account id. Locally: terraform init -backend-config=backend.hcl
  # (untracked). In CI: -backend-config="bucket=${{ vars.TF_STATE_BUCKET }}".
  backend "s3" {
    key          = "voice-agent/terraform.tfstate"
    region       = "eu-central-1"
    encrypt      = true
    use_lockfile = true
  }
}

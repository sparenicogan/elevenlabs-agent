terraform {
  backend "s3" {
    bucket       = "elevenlabs-agent-tfstate-199013204701"
    key          = "voice-agent/terraform.tfstate"
    region       = "eu-central-1"
    encrypt      = true
    use_lockfile = true
  }
}

terraform {
  required_version = ">= 1.6"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# The subscription ID is NOT written here. Terraform reads it from the
# ARM_SUBSCRIPTION_ID environment variable, so no IDs end up in Git.
provider "azurerm" {
  features {}
}
variable "project" {
  description = "Short project name used in every resource name. Lowercase letters and numbers only."
  type        = string
  default     = "webapp"

  validation {
    condition     = can(regex("^[a-z0-9]{3,10}$", var.project))
    error_message = "project must be 3-10 lowercase letters or numbers (it becomes part of the storage account name)."
  }
}

variable "environment" {
  description = "Environment name. Must match the current workspace (dev or prod)."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be \"dev\" or \"prod\"."
  }
}

variable "location" {
  description = "Azure region. Must be one your subscription is allowed to use."
  type        = string
}

variable "app_service_sku" {
  description = "App Service plan size. F1 is free; B1 is the smallest paid tier."
  type        = string
  default     = "F1"
}

variable "log_retention_days" {
  description = "How long Log Analytics keeps data."
  type        = number
  default     = 30
}

variable "enable_sql" {
  description = "Create an Azure SQL server and database."
  type        = bool
  default     = false
}

variable "enable_postgres" {
  description = "Create an Azure Database for PostgreSQL flexible server."
  type        = bool
  default     = false
}
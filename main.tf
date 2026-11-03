# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

# A random suffix keeps globally unique names (web app, storage, databases)
# from colliding with other students. Each workspace has its own state, so
# dev and prod get different suffixes.
resource "random_string" "suffix" {
  length  = 5
  upper   = false
  special = false
}

locals {
  name   = "${var.project}-${var.environment}"
  suffix = random_string.suffix.result

  tags = {
    project     = var.project
    environment = var.environment
    managed_by  = "terraform"
  }
}

# ---------------------------------------------------------------------------
# Resource group (rg)
# ---------------------------------------------------------------------------

resource "azurerm_resource_group" "rg" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags

  lifecycle {
    precondition {
      condition     = terraform.workspace == var.environment
      error_message = "Workspace and environment do not match. Run 'terraform workspace select ${var.environment}' or use the matching .tfvars file."
    }
  }
}

# ---------------------------------------------------------------------------
# Monitoring: Log Analytics workspace (law) + Application Insights (appi)
# ---------------------------------------------------------------------------

resource "azurerm_log_analytics_workspace" "law" {
  name                = "law-${local.name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = local.tags
}

resource "azurerm_application_insights" "appi" {
  name                = "appi-${local.name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  workspace_id        = azurerm_log_analytics_workspace.law.id
  application_type    = "web"
  tags                = local.tags
}

# ---------------------------------------------------------------------------
# App Service plan (asp) + web app
# ---------------------------------------------------------------------------

resource "azurerm_service_plan" "asp" {
  name                = "asp-${local.name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  os_type             = "Linux"
  sku_name            = var.app_service_sku
  tags                = local.tags
}

resource "azurerm_linux_web_app" "app" {
  name                = "app-${local.name}-${local.suffix}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  service_plan_id     = azurerm_service_plan.asp.id
  https_only          = true
  tags                = local.tags

  site_config {
    # The free tier (F1) does not support Always On.
    always_on = var.app_service_sku != "F1"

    application_stack {
      node_version = "22-lts"
    }
  }

  app_settings = {
    APPLICATIONINSIGHTS_CONNECTION_STRING = azurerm_application_insights.appi.connection_string
    ENVIRONMENT                           = var.environment
  }

  # Only added when the matching database is enabled.
  dynamic "connection_string" {
    for_each = var.enable_sql ? [1] : []
    content {
      name  = "SqlDb"
      type  = "SQLAzure"
      value = "Server=tcp:${azurerm_mssql_server.sql[0].fully_qualified_domain_name},1433;Database=${azurerm_mssql_database.sqldb[0].name};User ID=${azurerm_mssql_server.sql[0].administrator_login};Password=${random_password.sql[0].result};Encrypt=true;"
    }
  }

  dynamic "connection_string" {
    for_each = var.enable_postgres ? [1] : []
    content {
      name  = "PostgresDb"
      type  = "PostgreSQL"
      value = "Host=${azurerm_postgresql_flexible_server.psql[0].fqdn};Database=${azurerm_postgresql_flexible_server_database.psqldb[0].name};Username=${azurerm_postgresql_flexible_server.psql[0].administrator_login};Password=${random_password.psql[0].result};SslMode=Require"
    }
  }
}

# ---------------------------------------------------------------------------
# Storage account (st)
# ---------------------------------------------------------------------------

# Storage account names: 3-24 characters, lowercase letters and numbers only,
# globally unique. No hyphens allowed.
resource "azurerm_storage_account" "st" {
  name                            = "st${var.project}${var.environment}${local.suffix}"
  location                        = azurerm_resource_group.rg.location
  resource_group_name             = azurerm_resource_group.rg.name
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  tags                            = local.tags
}

# ---------------------------------------------------------------------------
# OPTIONAL: Azure SQL (sql + sqldb). Turn on with enable_sql = true.
# ---------------------------------------------------------------------------

resource "random_password" "sql" {
  count   = var.enable_sql ? 1 : 0
  length  = 24
  special = true
}

resource "azurerm_mssql_server" "sql" {
  count                        = var.enable_sql ? 1 : 0
  name                         = "sql-${local.name}-${local.suffix}"
  location                     = azurerm_resource_group.rg.location
  resource_group_name          = azurerm_resource_group.rg.name
  version                      = "12.0"
  administrator_login          = "sqladmin${var.environment}"
  administrator_login_password = random_password.sql[0].result
  minimum_tls_version          = "1.2"
  tags                         = local.tags
}

resource "azurerm_mssql_database" "sqldb" {
  count       = var.enable_sql ? 1 : 0
  name        = "sqldb-${local.name}"
  server_id   = azurerm_mssql_server.sql[0].id
  sku_name    = "Basic"
  max_size_gb = 2
  tags        = local.tags
}

# 0.0.0.0 to 0.0.0.0 is Azure's special rule meaning "allow Azure services",
# which lets the web app reach the database.
resource "azurerm_mssql_firewall_rule" "allow_azure" {
  count            = var.enable_sql ? 1 : 0
  name             = "AllowAzureServices"
  server_id        = azurerm_mssql_server.sql[0].id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# ---------------------------------------------------------------------------
# OPTIONAL: PostgreSQL flexible server (psql + psqldb). enable_postgres = true.
# ---------------------------------------------------------------------------

resource "random_password" "psql" {
  count   = var.enable_postgres ? 1 : 0
  length  = 24
  special = false
}

resource "azurerm_postgresql_flexible_server" "psql" {
  count                         = var.enable_postgres ? 1 : 0
  name                          = "psql-${local.name}-${local.suffix}"
  location                      = azurerm_resource_group.rg.location
  resource_group_name           = azurerm_resource_group.rg.name
  version                       = "16"
  sku_name                      = "B_Standard_B1ms"
  storage_mb                    = 32768
  administrator_login           = "psqladmin"
  administrator_password        = random_password.psql[0].result
  public_network_access_enabled = true
  tags                          = local.tags

  # Azure picks an availability zone if we don't. Ignoring it stops
  # Terraform from trying to "fix" the zone on every plan.
  lifecycle {
    ignore_changes = [zone]
  }
}

resource "azurerm_postgresql_flexible_server_database" "psqldb" {
  count     = var.enable_postgres ? 1 : 0
  name      = "appdb"
  server_id = azurerm_postgresql_flexible_server.psql[0].id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_azure" {
  count            = var.enable_postgres ? 1 : 0
  name             = "AllowAzureServices"
  server_id        = azurerm_postgresql_flexible_server.psql[0].id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}
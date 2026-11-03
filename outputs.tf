output "resource_group_name" {
  value = azurerm_resource_group.rg.name
}

output "web_app_name" {
  value = azurerm_linux_web_app.app.name
}

output "web_app_url" {
  value = "https://${azurerm_linux_web_app.app.default_hostname}"
}

output "storage_account_name" {
  value = azurerm_storage_account.st.name
}

output "sql_server_fqdn" {
  value = var.enable_sql ? azurerm_mssql_server.sql[0].fully_qualified_domain_name : null
}

output "postgres_server_fqdn" {
  value = var.enable_postgres ? azurerm_postgresql_flexible_server.psql[0].fqdn : null
}
output "alb_dns_name" {
  description = "Public entry point (resilience layer)"
  value       = aws_lb.main.dns_name
}

output "mock_internal_url" {
  description = "Internal-only URL of the mock (resolves to private IPs)"
  value       = local.mock_url
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "resilience_service" {
  value = aws_ecs_service.resilience.name
}

output "mock_service" {
  value = aws_ecs_service.mock.name
}

output "blue_green_enabled" {
  value = var.enable_blue_green
}

output "dashboard_name" {
  value = aws_cloudwatch_dashboard.main.dashboard_name
}
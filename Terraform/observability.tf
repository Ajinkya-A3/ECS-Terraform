resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${local.name}-overview"

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "text", x = 0, y = 0, width = 24, height = 1
        properties = { markdown = "# ${local.name}: traffic, errors, latency, scaling" }
      },

      # Request rate (public ALB)
      {
        type = "metric", x = 0, y = 1, width = 12, height = 6
        properties = {
          title   = "Requests per minute"
          region  = var.region
          view    = "timeSeries"
          stat    = "Sum"
          period  = 60
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.main.arn_suffix]
          ]
        }
      },

      # Error rate (metric math). FILL keeps the line alive when a 5xx series has no data.
      {
        type = "metric", x = 12, y = 1, width = 12, height = 6
        properties = {
          title  = "5xx error rate (%)"
          region = var.region
          view   = "timeSeries"
          period = 60
          yAxis  = { left = { min = 0 } }
          metrics = [
            [{ expression = "IF(req > 0, 100 * (FILL(t5, 0) + FILL(e5, 0)) / req, 0)", label = "5xx %", id = "rate" }],
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.main.arn_suffix, { id = "req", stat = "Sum", visible = false }],
            [".", "HTTPCode_Target_5XX_Count", ".", ".", { id = "t5", stat = "Sum", visible = false }],
            [".", "HTTPCode_ELB_5XX_Count", ".", ".", { id = "e5", stat = "Sum", visible = false }]
          ]
        }
      },

      # Latency percentiles (TargetResponseTime is in seconds)
      {
        type = "metric", x = 0, y = 7, width = 12, height = 6
        properties = {
          title  = "Latency (seconds)"
          region = var.region
          view   = "timeSeries"
          period = 60
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", aws_lb.main.arn_suffix, { stat = "p50", label = "p50" }],
            ["...", { stat = "p95", label = "p95" }],
            ["...", { stat = "p99", label = "p99" }]
          ]
        }
      },

      # Task counts: where autoscaling becomes visible (Container Insights: ServiceName + ClusterName)
      {
        type = "metric", x = 12, y = 7, width = 12, height = 6
        properties = {
          title  = "Resilience-layer tasks"
          region = var.region
          view   = "timeSeries"
          stat   = "Average"
          period = 60
          metrics = [
            ["ECS/ContainerInsights", "DesiredTaskCount", "ServiceName", aws_ecs_service.resilience.name, "ClusterName", aws_ecs_cluster.main.name],
            [".", "RunningTaskCount", ".", ".", ".", "."]
          ]
        }
      },

      # Circuit breaker / retry / dead-letter log lines
      {
        type = "log", x = 0, y = 13, width = 24, height = 6
        properties = {
          title  = "Circuit breaker and DLQ events"
          region = var.region
          view   = "table"
          query  = "SOURCE '${aws_cloudwatch_log_group.resilience.name}' | fields @timestamp, @message | filter @message like /circuit|dead.?letter|retry/ | sort @timestamp desc | limit 50"
        }
      }
    ]
  })
}
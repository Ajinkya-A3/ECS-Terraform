resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "resilience" {
  name              = "/ecs/${local.name}/resilience-layer"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "mock" {
  name              = "/ecs/${local.name}/mock-http"
  retention_in_days = var.log_retention_days
}

locals {
  mock_url = "http://${aws_lb.mock.dns_name}"

  resilience_env = merge(
    {
      PORT            = tostring(var.resilience_port)
      DEPENDENCY_URL  = local.mock_url
      DEPENDENCY_KIND = "mock"
    },
    var.resilience_env
  )

  mock_env = merge({ PORT = tostring(var.mock_port) }, var.mock_env)
}

# ---------------- resilience-layer (public ALB) ----------------
resource "aws_ecs_task_definition" "resilience" {
  family                   = "${local.name}-resilience-layer"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.resilience_cpu
  memory                   = var.resilience_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.resilience_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name         = "resilience-layer"
      image        = var.resilience_image
      essential    = true
      portMappings = [{ containerPort = var.resilience_port, protocol = "tcp" }]
      environment  = [for k, v in local.resilience_env : { name = k, value = v }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.resilience.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "resilience"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "resilience" {
  name            = "resilience-layer"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.resilience.arn
  desired_count   = var.resilience_desired_count
  launch_type     = "FARGATE"

  health_check_grace_period_seconds = 30

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.resilience.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.tg["blue"].arn
    container_name   = "resilience-layer"
    container_port   = var.resilience_port

    dynamic "advanced_configuration" {
      for_each = var.enable_blue_green ? [1] : []
      content {
        alternate_target_group_arn = aws_lb_target_group.tg["green"].arn
        production_listener_rule   = aws_lb_listener_rule.app.arn
        role_arn                   = aws_iam_role.ecs_infrastructure[0].arn
      }
    }
  }

  # Blue/green ON (bake_time_in_minutes is required for BLUE_GREEN)
  dynamic "deployment_configuration" {
    for_each = var.enable_blue_green ? [1] : []
    content {
      strategy             = "BLUE_GREEN"
      bake_time_in_minutes = var.bake_time_minutes
    }
  }

  # Blue/green OFF: rolling update with automatic rollback (circuit breaker is rolling-only)
  dynamic "deployment_circuit_breaker" {
    for_each = var.enable_blue_green ? [] : [1]
    content {
      enable   = true
      rollback = true
    }
  }

  # Autoscaling (added later) owns the running count.
  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [
    aws_lb_listener_rule.app,
    aws_iam_role_policy.execution,
    aws_iam_role_policy_attachment.ecs_infrastructure,
    aws_ecs_service.mock,
  ]
}

# ---------------- mock-http (internal ALB) ----------------
resource "aws_ecs_task_definition" "mock" {
  family                   = "${local.name}-mock-http"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.mock_cpu
  memory                   = var.mock_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.mock_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name         = "mock-http"
      image        = var.mock_image
      essential    = true
      portMappings = [{ containerPort = var.mock_port, protocol = "tcp" }]
      environment  = [for k, v in local.mock_env : { name = k, value = v }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.mock.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "mock"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "mock" {
  name            = "mock-http"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.mock.arn
  desired_count   = var.mock_desired_count
  launch_type     = "FARGATE"

  health_check_grace_period_seconds = 30

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.mock.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.mock["blue"].arn
    container_name   = "mock-http"
    container_port   = var.mock_port

    dynamic "advanced_configuration" {
      for_each = var.enable_blue_green ? [1] : []
      content {
        alternate_target_group_arn = aws_lb_target_group.mock["green"].arn
        production_listener_rule   = aws_lb_listener_rule.mock.arn
        role_arn                   = aws_iam_role.ecs_infrastructure[0].arn
      }
    }
  }

  dynamic "deployment_configuration" {
    for_each = var.enable_blue_green ? [1] : []
    content {
      strategy             = "BLUE_GREEN"
      bake_time_in_minutes = var.bake_time_minutes
    }
  }

  dynamic "deployment_circuit_breaker" {
    for_each = var.enable_blue_green ? [] : [1]
    content {
      enable   = true
      rollback = true
    }
  }

  depends_on = [
    aws_lb_listener_rule.mock,
    aws_iam_role_policy.execution,
    aws_iam_role_policy_attachment.ecs_infrastructure,
  ]
}
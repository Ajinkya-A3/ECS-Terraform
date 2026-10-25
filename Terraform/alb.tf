resource "aws_lb" "main" {
  name               = "${local.name}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
}

# Two target groups, always created. Blue/green swaps which one is live.
# Target type must be "ip" for Fargate.
resource "aws_lb_target_group" "tg" {
  for_each = toset(["blue", "green"])

  name        = "${local.name}-${each.key}"
  port        = var.resilience_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  deregistration_delay = 30

  health_check {
    path                = var.resilience_health_path
    matcher             = "200-399"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "not found"
      status_code  = "404"
    }
  }
}

# Production rule. For blue/green it must forward to BOTH target groups
# (100 on live, 0 on the other); ECS rewrites the weights during a deployment,
# so Terraform must not revert them.
resource "aws_lb_listener_rule" "app" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 100

  action {
    type = "forward"

    forward {
      target_group {
        arn    = aws_lb_target_group.tg["blue"].arn
        weight = 100
      }
      target_group {
        arn    = aws_lb_target_group.tg["green"].arn
        weight = 0
      }
    }
  }

  condition {
    path_pattern {
      values = ["/*"]
    }
  }

  lifecycle {
    ignore_changes = [action[0].forward]
  }
}
# Internal ALB in front of mock-http. Private subnets, reachable only from the resilience layer's SG.
resource "aws_lb" "mock" {
  name               = "${local.name}-mock-alb"
  load_balancer_type = "application"
  internal           = true
  security_groups    = [aws_security_group.mock_alb.id]
  subnets            = aws_subnet.private[*].id
}

resource "aws_lb_target_group" "mock" {
  for_each = toset(["blue", "green"])

  name        = "${local.name}-mock-${each.key}"
  port        = var.mock_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  deregistration_delay = 30

  health_check {
    path                = var.mock_health_path
    matcher             = "200-399"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "mock" {
  load_balancer_arn = aws_lb.mock.arn
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

# Must forward to BOTH target groups (100 live / 0 other); ECS rewrites the weights during blue/green.
resource "aws_lb_listener_rule" "mock" {
  listener_arn = aws_lb_listener.mock.arn
  priority     = 100

  action {
    type = "forward"

    forward {
      target_group {
        arn    = aws_lb_target_group.mock["blue"].arn
        weight = 100
      }
      target_group {
        arn    = aws_lb_target_group.mock["green"].arn
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
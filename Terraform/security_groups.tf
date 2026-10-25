resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public ALB"
  vpc_id      = aws_vpc.main.id

  lifecycle {
    ignore_changes = [ingress, egress]
  }
}

resource "aws_security_group" "resilience" {
  name        = "${local.name}-resilience"
  description = "Resilience layer tasks"
  vpc_id      = aws_vpc.main.id

  lifecycle {
    ignore_changes = [ingress, egress]
  }
}

resource "aws_security_group" "mock_alb" {
  name        = "${local.name}-mock-alb"
  description = "Internal ALB in front of mock-http"
  vpc_id      = aws_vpc.main.id

  lifecycle {
    ignore_changes = [ingress, egress]
  }
}

resource "aws_security_group" "mock" {
  name        = "${local.name}-mock"
  description = "Mock HTTP tasks"
  vpc_id      = aws_vpc.main.id

  lifecycle {
    ignore_changes = [ingress, egress]
  }
}

# Internet -> public ALB
resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

# public ALB -> resilience (traffic and health checks)
resource "aws_vpc_security_group_egress_rule" "alb_to_resilience" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.resilience.id
  from_port                    = var.resilience_port
  to_port                      = var.resilience_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "resilience_from_alb" {
  security_group_id            = aws_security_group.resilience.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.resilience_port
  to_port                      = var.resilience_port
  ip_protocol                  = "tcp"
}

# resilience -> internal mock ALB (listener port 80). The ONLY way in to the mock.
resource "aws_vpc_security_group_ingress_rule" "mock_alb_from_resilience" {
  security_group_id            = aws_security_group.mock_alb.id
  referenced_security_group_id = aws_security_group.resilience.id
  from_port                    = 80
  to_port                      = 80
  ip_protocol                  = "tcp"
}

# internal mock ALB -> mock tasks (traffic and health checks)
resource "aws_vpc_security_group_egress_rule" "mock_alb_to_mock" {
  security_group_id            = aws_security_group.mock_alb.id
  referenced_security_group_id = aws_security_group.mock.id
  from_port                    = var.mock_port
  to_port                      = var.mock_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "mock_from_mock_alb" {
  security_group_id            = aws_security_group.mock.id
  referenced_security_group_id = aws_security_group.mock_alb.id
  from_port                    = var.mock_port
  to_port                      = var.mock_port
  ip_protocol                  = "tcp"
}

# Task egress: Docker Hub pulls via NAT, CloudWatch Logs
resource "aws_vpc_security_group_egress_rule" "resilience_all" {
  security_group_id = aws_security_group.resilience.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_egress_rule" "mock_all" {
  security_group_id = aws_security_group.mock.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
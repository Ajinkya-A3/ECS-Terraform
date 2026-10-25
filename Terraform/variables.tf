variable "region" {
  type    = string
  default = "ap-south-1"
}

# Keep project + environment short: ALB and target group names are capped at 32 chars.
# Longest generated name is "<project>-<env>-mock-green".
variable "project" {
  type    = string
  default = "voice-agent"
}

variable "environment" {
  type    = string
  default = "dev"
}

# ---------- network ----------
variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "az_count" {
  type    = number
  default = 2
}

variable "single_nat_gateway" {
  description = "true = one NAT (cheap), false = one per AZ (HA)"
  type        = bool
  default     = true
}

# ---------- images (public Docker Hub) ----------
variable "resilience_image" {
  description = "e.g. youruser/resilience-layer:1.0.0 (pin tags, avoid :latest)"
  type        = string
}

variable "mock_image" {
  description = "e.g. youruser/mock-http:1.0.0"
  type        = string
}

variable "cpu_architecture" {
  type    = string
  default = "X86_64" # ARM64 only if your images are built for arm64
}

# ---------- resilience layer ----------
variable "resilience_port" {
  type    = number
  default = 8000
}

variable "resilience_cpu" {
  type    = number
  default = 512
}

variable "resilience_memory" {
  type    = number
  default = 1024
}

variable "resilience_desired_count" {
  type    = number
  default = 2
}

variable "resilience_health_path" {
  type    = string
  default = "/health"
}

variable "resilience_env" {
  description = "Extra env vars for the resilience layer"
  type        = map(string)
  default     = {}
}

# ---------- mock ----------
variable "mock_port" {
  type    = number
  default = 8080
}

variable "mock_cpu" {
  type    = number
  default = 256
}

variable "mock_memory" {
  type    = number
  default = 512
}

variable "mock_desired_count" {
  type    = number
  default = 2
}

variable "mock_health_path" {
  description = "Health endpoint on the mock; its ALB target groups check it"
  type        = string
  default     = "/health"
}

variable "mock_env" {
  type    = map(string)
  default = {}
}

# ---------- deployment ----------
variable "enable_blue_green" {
  description = "true = ECS-native blue/green for BOTH services, false = rolling update with circuit breaker for both"
  type        = bool
  default     = false
}

variable "bake_time_minutes" {
  description = "Blue/green only: how long blue and green both run after traffic shifts (0-1440)"
  type        = number
  default     = 5
}

variable "log_retention_days" {
  type    = number
  default = 14
}
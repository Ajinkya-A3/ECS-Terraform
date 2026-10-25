# ECS Teraform

Terraform for a two-service system on **AWS ECS Fargate** in `ap-south-1`:

- **resilience-layer**: the public-facing service (retries, circuit breaker, dead-letter queue). Reached through a public ALB.
- **mock-http**: a mock STT/LLM/TTS dependency with an injected failure rate. **Private**: reachable only from the resilience layer, through an internal ALB.

Images come from public Docker Hub. There is no GPU. A single variable switches both services between **ECS-native blue/green** and a normal **rolling update**.

## Architecture

```
                 Internet
                    │ :80
            ┌───────▼────────┐   public subnets (2 AZs)
            │   Public ALB   │
            └───────┬────────┘
                    │ :8000
 ───────────────────┼───────────────────────────── private subnets (2 AZs)
            ┌───────▼────────┐
            │ resilience-    │──── outbound via NAT (Docker Hub pulls, CloudWatch)
            │ layer (Fargate)│
            └───────┬────────┘
                    │ :80  (PIPELINE_URL)
            ┌───────▼────────┐
            │  Internal ALB  │  (private, no public IP)
            └───────┬────────┘
                    │ :9000
            ┌───────▼────────┐
            │ mock-http      │
            │ (Fargate)      │
            └────────────────┘
```

Each service has a **blue** and a **green** target group. Rolling mode only uses blue. Blue/green mode swaps between them.

## Repository layout

```
.
├── versions.tf               # Terraform + AWS provider versions, default tags
├── variables.tf              # All inputs and defaults
├── network.tf                # VPC, subnets, IGW, NAT, route tables
├── security_groups.tf        # 4 SGs + standalone ingress/egress rules
├── alb.tf                    # Public ALB, blue/green target groups, listener + rule
├── alb_mock.tf               # Internal ALB for the mock (same pattern)
├── iam.tf                    # Execution role, task roles, ECS infrastructure role
├── ecs.tf                    # Cluster, log groups, task definitions, services
├── observability.tf          # CloudWatch dashboard
├── outputs.tf                # ALB DNS, internal URL, names
├── terraform.tfvars.example  # Template for your values
└── .gitignore
```

## How it is built, file by file

**`network.tf`** creates one VPC (`10.0.0.0/16`) with DNS support and hostnames on. For each of `az_count` AZs (default 2) it makes a public and a private subnet. Public subnets route to an internet gateway. Private subnets route `0.0.0.0/0` to a NAT gateway so tasks can pull images and ship logs without public IPs. `single_nat_gateway = true` (default) uses one NAT to save cost. `false` gives one per AZ.

**`security_groups.tf`** defines four groups and wires them so only the intended path exists:

| From | To | Port |
|---|---|---|
| Internet | Public ALB | 80 |
| Public ALB | resilience tasks | 8000 |
| resilience tasks | Internal ALB | 80 |
| Internal ALB | mock tasks | 9000 |

Nothing else can reach the mock. Rules are standalone `aws_vpc_security_group_*_rule` resources, and the groups use `ignore_changes = [ingress, egress]` so inline rules never fight them.

**`alb.tf` / `alb_mock.tf`** each create an ALB, two IP-type target groups (`blue`, `green`), an HTTP:80 listener with a 404 default, and one `/*` listener rule. That rule forwards to **both** target groups (100/0 weights). ECS-native blue/green requires this, and ECS rewrites the weights during a deployment, so the rule has `ignore_changes = [action[0].forward]`. The mock ALB is `internal = true` and sits in the private subnets.

**`iam.tf`** creates:
- an **execution role** (writes to the two log groups only; no wildcard actions),
- one empty **task role** per service (the app code gets no AWS permissions yet),
- an **ECS infrastructure role** with `AmazonECSInfrastructureRolePolicyForLoadBalancers`, created only when blue/green is on. ECS assumes it to shift ALB traffic.

**`ecs.tf`** creates the cluster (Container Insights on), two log groups, two Fargate task definitions and two services. Both services run in private subnets with no public IP.
- The resilience service attaches to the public ALB's blue target group. The mock service attaches to the internal ALB's blue target group.
- The deployment strategy is chosen by `enable_blue_green` (see below).
- `desired_count` is ignored on the resilience service so autoscaling can own it later.

**`observability.tf`** creates one CloudWatch dashboard: requests per minute, 5xx error rate (metric math), latency p50/p95/p99, task counts (Container Insights), and a Logs Insights table for circuit-breaker and dead-letter lines.

## How the services find each other

The mock has no public path and no fixed IP. Terraform builds the URL from the internal ALB's DNS name and injects it into the resilience task:

```
PIPELINE_BACKEND = http
PIPELINE_URL     = http://<internal-alb-dns-name>      # port 80 → mock :9000
```

No Route 53 domain is needed: ALB DNS names are AWS-generated and resolve to private IPs inside the VPC. All other app settings come from `resilience_env` and `mock_env` in tfvars.

## Deployment modes (`enable_blue_green`)

| | `false` (default) | `true` |
|---|---|---|
| Strategy | Rolling update | ECS-native blue/green |
| Rollback | Deployment circuit breaker | Keep blue running for `bake_time_minutes`, roll back by shifting traffic |
| Extra resources | none | ECS infrastructure role + attachment |
| Capacity during deploy | up to 200% | up to 2× tasks per service |

The circuit breaker is rolling-only, so it is automatically off in blue/green mode. The setting applies to **both** services.

## Prerequisites

- Terraform `>= 1.6`, AWS provider `~> 6.4` (native blue/green needs 6.4.0+)
- AWS credentials with permission to create the resources above, including `iam:PassRole` on the infrastructure role for `ecs.amazonaws.com`
- Two **public** Docker Hub images (built for the CPU architecture you set)

## Usage

```bash
cp terraform.tfvars.example terraform.tfvars   # edit values
terraform init
terraform fmt
terraform validate
terraform plan -out=tfplan
terraform apply tfplan
```

Smoke test:

```bash
curl http://$(terraform output -raw alb_dns_name)/health
```

## Configuration

Key variables (see `variables.tf` for all):

| Variable | Default | Notes |
|---|---|---|
| `region` | `ap-south-1` | |
| `project`, `environment` | `voice-agent`, `dev` | Keep short: ALB/target-group names are capped at 32 chars |
| `resilience_image`, `mock_image` | required | Pin versioned tags |
| `cpu_architecture` | `X86_64` | `ARM64` if images were built for arm64 |
| `resilience_port`, `mock_port` | `8000`, `8080` | Set `mock_port = 9000` for this app |
| `resilience_health_path`, `mock_health_path` | `/health` | Must return 2xx/3xx |
| `enable_blue_green` | `false` | |
| `bake_time_minutes` | `5` | Blue/green only |
| `single_nat_gateway` | `true` | `false` for per-AZ NAT |
| `resilience_env`, `mock_env` | `{}` | App env vars; values must be strings |

Example `terraform.tfvars`:

```hcl
region      = "ap-south-1"
project     = "voice-agent"
environment = "dev"

resilience_image = "at1asflame/ecs-test:service"
mock_image       = "at1asflame/ecs-test:mock"

resilience_port        = 8000
resilience_health_path = "/health"
mock_port              = 9000
mock_health_path       = "/health"

cpu_architecture  = "X86_64"
enable_blue_green = false
bake_time_minutes = 5

mock_env = {
  MOCK_FAIL_RATE = "0.20"
  MOCK_SIGMA     = "0.6"
}

resilience_env = {
  REQUEST_TIMEOUT_S = "3"
  RETRY_ATTEMPTS    = "3"
  RETRY_INITIAL_S   = "0.2"
  RETRY_MAX_S       = "2.0"
  BREAKER_FAIL_MAX  = "5"
  BREAKER_RESET_S   = "15"
  DLQ_PATH          = "/data/dlq.db"
}
```

## Deploying a new version

1. Push a new image with a **new tag** (for example `:service-1.0.1`).
2. Update the image in `terraform.tfvars`.
3. `terraform plan` and `terraform apply`.

Reusing a tag like `:service` creates no task-definition change, so nothing redeploys. Changing any env var also redeploys, because it creates a new task-definition revision.

## Operating notes

- **Toggling blue/green on an existing deployment** may be rejected in place. If so: `terraform apply -replace=aws_ecs_service.resilience -replace=aws_ecs_service.mock`.
- **After the first blue/green swap**, check `terraform plan` for a diff on each service's `load_balancer.target_group_arn`. If it appears, add `load_balancer` to that service's `lifecycle.ignore_changes`.
- **Don't edit or delete the infrastructure role** during a blue/green deployment.
- **If the first deployment fails health checks**, the circuit breaker has nothing to roll back to. Check the service events in the ECS console.
- **Mock health vs failure rate**: the mock's `/health` must be exempt from `MOCK_FAIL_RATE`, or the ALB will flap targets.

## Known limitations

- The dead-letter database (`/data/dlq.db`) lives on each task's ephemeral disk. It is lost when a task is replaced and is not shared between tasks. Use EFS or SQS for real durability.
- HTTP only (port 80). Add an ACM certificate and a 443 listener for HTTPS.
- Anonymous Docker Hub pulls are rate-limited per source IP, and all tasks share the NAT's IP. If a pull fails on the limit, wait or mirror the images into ECR.
- Autoscaling is not included yet.

## Cost and teardown

The fixed hourly costs are **two ALBs and the NAT gateway**, plus Fargate task time. Destroy everything when you are done:

```bash
terraform destroy
```

Confirm in the console that no ALB, NAT gateway or Elastic IP remains.

## Reference docs

- [ECS blue/green with ALB](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/alb-resources-for-blue-green.html)
- [ECS infrastructure role for load balancers](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AmazonECSInfrastructureRolePolicyForLoadBalancers.html)
- [Container Insights metrics for ECS](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-metrics-ECS.html)
# Enabling GPU for the inference service

Fargate cannot run GPU workloads. ECS runs GPU tasks on **EC2 container instances** using the ECS GPU-optimized AMI. This guide adds an optional `enable_gpu` toggle:

| | `enable_gpu = false` (default) | `enable_gpu = true` |
|---|---|---|
| resilience-layer | Fargate | Fargate (unchanged) |
| mock-http (inference) | Fargate | **EC2 GPU instances** via an ECS capacity provider |
| Extra resources | none | launch template, Auto Scaling group, capacity provider, instance role |

The inference service stays behind the same internal ALB, so `PIPELINE_URL` and the security groups do not change.

## How it works

```
Pending task needing a GPU
        │
        ▼
ECS capacity provider (managed scaling)
        │ raises desired capacity
        ▼
Auto Scaling group (private subnets) ── launches ──► g4dn/g5 instance
                                                      │ ECS GPU-optimized AMI
                                                      │ user data: ECS_CLUSTER=<cluster>
                                                      ▼
                                          instance registers with the cluster
                                                      │
                                                      ▼
                       ECS places the task and pins the requested GPU(s) to the container
                       (resourceRequirements: type=GPU)
```

The GPU-optimized AMI includes the NVIDIA drivers and the Docker GPU runtime. ECS sets the NVIDIA runtime and `NVIDIA_VISIBLE_DEVICES` for any container that requests a GPU.

## Step 0: Prerequisites in your AWS account

### 1. Check GPU instance types exist in ap-south-1
```bash
aws ec2 describe-instance-type-offerings --region ap-south-1 \
  --location-type availability-zone \
  --filters Name=instance-type,Values=g4dn.xlarge,g5.xlarge,g6.xlarge \
  --query 'InstanceTypeOfferings[].[InstanceType,Location]' --output table
```
Pick a type offered in **both** AZs you use, or the Auto Scaling group may fail to launch in one of them.

### 2. Raise the GPU vCPU quota
GPU instances use a separate On-Demand quota ("Running On-Demand G and VT instances"). New accounts often have a very low or zero value. A `g4dn.xlarge` needs 4 vCPUs.
```bash
# find the quota code and current value
aws service-quotas list-service-quotas --service-code ec2 --region ap-south-1 \
  --query "Quotas[?contains(QuotaName,'G and VT')].[QuotaName,QuotaCode,Value]" --output table

# request an increase (replace the code and value)
aws service-quotas request-service-quota-increase --service-code ec2 \
  --quota-code <QUOTA_CODE> --desired-value 16 --region ap-south-1
```
Size it for peak, not just steady state (see "Deployments need spare GPUs" below). Approval can take time, so request it first.

### 3. Check the AMI parameter
```bash
aws ssm get-parameters --region ap-south-1 \
  --names /aws/service/ecs/optimized-ami/amazon-linux-2023/gpu/recommended/image_id \
  --query 'Parameters[0].Value' --output text
```
This should print an `ami-...` ID. The docs document the `.../gpu/recommended` parameter. The `/image_id` suffix follows the same pattern, so this command confirms it exists.

## Step 1: New variables (add to `variables.tf`)

```hcl
variable "enable_gpu" {
  description = "true = run mock-http (inference) on EC2 GPU instances instead of Fargate"
  type        = bool
  default     = false
}

variable "gpu_instance_type" {
  type    = string
  default = "g4dn.xlarge"
}

variable "gpu_count" {
  description = "GPUs reserved per inference task"
  type        = number
  default     = 1
}

variable "gpu_min_size" {
  description = "Minimum GPU instances. 0 saves money but the first task waits for an instance to boot"
  type        = number
  default     = 1
}

variable "gpu_max_size" {
  description = "Maximum GPU instances. Keep this at least 2x what steady state needs (see deployments note)"
  type        = number
  default     = 2
}

variable "gpu_root_volume_gb" {
  description = "Root disk size. Model images are large"
  type        = number
  default     = 100
}
```

## Step 2: New file `gpu.tf`

```hcl
# ---------- AMI: ECS GPU-optimized, Amazon Linux 2023 ----------
data "aws_ssm_parameter" "ecs_gpu_ami" {
  count = var.enable_gpu ? 1 : 0
  name  = "/aws/service/ecs/optimized-ami/amazon-linux-2023/gpu/recommended/image_id"
}

# ---------- instance role (lets the ECS agent register and pull) ----------
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gpu_instance" {
  count              = var.enable_gpu ? 1 : 0
  name               = "${local.name}-gpu-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "gpu_instance" {
  count      = var.enable_gpu ? 1 : 0
  role       = aws_iam_role.gpu_instance[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "gpu" {
  count = var.enable_gpu ? 1 : 0
  name  = "${local.name}-gpu"
  role  = aws_iam_role.gpu_instance[0].name
}

# ---------- host security group (agent traffic only; tasks use their own ENI + the mock SG) ----------
resource "aws_security_group" "gpu_host" {
  count       = var.enable_gpu ? 1 : 0
  name        = "${local.name}-gpu-host"
  description = "GPU container instances"
  vpc_id      = aws_vpc.main.id

  lifecycle {
    ignore_changes = [ingress, egress]
  }
}

resource "aws_vpc_security_group_egress_rule" "gpu_host_all" {
  count             = var.enable_gpu ? 1 : 0
  security_group_id = aws_security_group.gpu_host[0].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ---------- launch template ----------
resource "aws_launch_template" "gpu" {
  count         = var.enable_gpu ? 1 : 0
  name_prefix   = "${local.name}-gpu-"
  image_id      = data.aws_ssm_parameter.ecs_gpu_ami[0].value
  instance_type = var.gpu_instance_type

  iam_instance_profile {
    arn = aws_iam_instance_profile.gpu[0].arn
  }

  vpc_security_group_ids = [aws_security_group.gpu_host[0].id]

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = var.gpu_root_volume_gb
      volume_type = "gp3"
      encrypted   = true
    }
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  user_data = base64encode(<<-EOT
    #!/bin/bash
    echo "ECS_CLUSTER=${aws_ecs_cluster.main.name}" >> /etc/ecs/ecs.config
  EOT
  )
}

# ---------- Auto Scaling group ----------
resource "aws_autoscaling_group" "gpu" {
  count               = var.enable_gpu ? 1 : 0
  name                = "${local.name}-gpu"
  min_size            = var.gpu_min_size
  max_size            = var.gpu_max_size
  vpc_zone_identifier = aws_subnet.private[*].id
  force_delete        = true # so terraform destroy never hangs on running instances

  launch_template {
    id      = aws_launch_template.gpu[0].id
    version = "$Latest"
  }

  tag {
    key                 = "AmazonECSManaged"
    value               = "true"
    propagate_at_launch = true
  }

  tag {
    key                 = "Name"
    value               = "${local.name}-gpu"
    propagate_at_launch = true
  }
}

# ---------- capacity provider ----------
resource "aws_ecs_capacity_provider" "gpu" {
  count = var.enable_gpu ? 1 : 0
  name  = "${local.name}-gpu"

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.gpu[0].arn
    managed_termination_protection = "DISABLED" # keeps teardown clean

    managed_scaling {
      status                    = "ENABLED"
      target_capacity           = 100
      minimum_scaling_step_size = 1
      maximum_scaling_step_size = 1
    }
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  count              = var.enable_gpu ? 1 : 0
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", aws_ecs_capacity_provider.gpu[0].name]
}
```

## Step 3: Change the inference task and service in `ecs.tf`

Replace the existing `aws_ecs_task_definition.mock` and `aws_ecs_service.mock` with these. The only differences are the GPU-conditional lines.

```hcl
resource "aws_ecs_task_definition" "mock" {
  family                   = "${local.name}-mock-http"
  requires_compatibilities = var.enable_gpu ? ["EC2"] : ["FARGATE"]
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

      # Reserves GPU(s) for this container. An empty list = no GPU.
      resourceRequirements = var.enable_gpu ? [{ type = "GPU", value = tostring(var.gpu_count) }] : []

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

  # Fargate when GPU is off; capacity provider (EC2 GPU) when on
  launch_type = var.enable_gpu ? null : "FARGATE"

  dynamic "capacity_provider_strategy" {
    for_each = var.enable_gpu ? [1] : []
    content {
      capacity_provider = aws_ecs_capacity_provider.gpu[0].name
      weight            = 1
    }
  }

  # Models take longer to load than a mock
  health_check_grace_period_seconds = var.enable_gpu ? 300 : 30

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
    aws_ecs_cluster_capacity_providers.main,
  ]
}
```

## Step 4: Set the values in `terraform.tfvars`

```hcl
enable_gpu        = true
gpu_instance_type = "g4dn.xlarge"
gpu_count         = 1
gpu_min_size      = 1
gpu_max_size      = 2

# Your inference image: it must be built on an NVIDIA/CUDA base (or set
# NVIDIA_DRIVER_CAPABILITIES) and for x86_64 if you use g4dn/g5
mock_image       = "youruser/your-model-image:1.0.0"
mock_port        = 9000
mock_cpu         = 2048
mock_memory      = 8192
mock_desired_count = 1

cpu_architecture = "X86_64"
```
Task `cpu`/`memory` must fit inside the instance. A `g4dn.xlarge` has 4 vCPUs and 16 GiB, and the ECS agent reserves some of it.

## Step 5: Apply and verify

```bash
terraform plan -out=tfplan   # review: launch template, ASG, capacity provider appear
terraform apply tfplan
```

Check that the instance joined and exposes its GPU:
```bash
CLUSTER=$(terraform output -raw cluster_name)
aws ecs list-container-instances --cluster $CLUSTER --region ap-south-1

aws ecs describe-container-instances --cluster $CLUSTER --region ap-south-1 \
  --container-instances <ARN_FROM_ABOVE> \
  --query "containerInstances[].registeredResources[?name=='GPU']"
```
A registered `GPU` resource with a non-empty value means the instance is GPU-ready. Then watch the service (`aws ecs describe-services ...`) until the task is RUNNING and the internal ALB target is healthy.

## Deployments need spare GPUs

During a deployment the old and new tasks run together. With **blue/green** that is 2x the GPUs. With the default **rolling update** (100% min healthy, 200% max) it is also up to 2x. If `gpu_max_size` only covers steady state, the new task sits in PENDING and the deployment stalls.

- Set `gpu_max_size` to at least 2x your steady-state instance count.
- Make sure the GPU quota covers that peak.
- Each `awsvpc` task uses one network interface, so also check the instance type's ENI limit when planning tasks per instance:
  ```bash
  aws ec2 describe-instance-types --instance-types g4dn.xlarge --region ap-south-1 \
    --query 'InstanceTypes[].NetworkInfo.MaximumNetworkInterfaces'
  ```

## Switching an existing deployment

Changing the service from `FARGATE` to a capacity provider replaces the service, so expect a short outage of the inference tier. If Terraform rejects an in-place change, use:
```bash
terraform apply -replace=aws_ecs_service.mock
```

## Troubleshooting

| Symptom | Likely cause and check |
|---|---|
| Task stuck `PENDING`, event mentions no container instance with GPU resources | No registered GPU instance yet, or not enough GPUs. Check the ASG and `describe-container-instances` above |
| ASG launches nothing | Quota is too low or the type isn't offered in an AZ. Run `aws autoscaling describe-scaling-activities --auto-scaling-group-name <name>` and read the failure reason |
| Instance runs but never registers | No route to ECS (check NAT), wrong `ECS_CLUSTER` in user data, or the instance role isn't attached |
| `exec format error` | Image architecture doesn't match `cpu_architecture` (g5g instances are ARM; g4dn/g5 are x86_64) |
| Container starts, no GPU visible | The image isn't NVIDIA/CUDA-based. Set `NVIDIA_DRIVER_CAPABILITIES` to `utility,compute` or `all` |
| Targets unhealthy while the model loads | Increase `health_check_grace_period_seconds` (set to 300 above) |
| Docker image pull is slow or fails | Large image through the NAT; also see Docker Hub anonymous pull limits |

## Cost and teardown

GPU instances are the most expensive part of this stack and bill for every hour they exist, even idle. Setting `gpu_min_size = 0` stops the idle cost but the first task then waits for an instance to boot. After a demo:

```bash
terraform destroy
```
`force_delete = true` on the Auto Scaling group and disabled managed termination protection let the destroy finish cleanly. Then confirm in the EC2 console that no GPU instance, ALB, NAT gateway or Elastic IP remains.

## Notes

- To run the **resilience layer** on GPU instead, apply the same changes to its task definition and service. It normally doesn't need one.
- The instance role uses the AWS-managed `AmazonEC2ContainerServiceforEC2Role` policy. If you need tighter scoping, replace it with a custom policy listing only the ECS agent's actions.
- Sharing one GPU between several containers works differently (remove the GPU requirement and make `nvidia` the default Docker runtime). That isn't set up here.

## References
- [ECS task definitions for GPU workloads](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-gpu.html)
- [Specifying GPUs in a task definition](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-gpu-specifying.html)
- [Retrieving ECS-optimized AMI metadata (SSM parameters)](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/retrieve-ecs-optimized_AMI.html)
- [Launch a GPU container instance for ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/gpu-launch.html)
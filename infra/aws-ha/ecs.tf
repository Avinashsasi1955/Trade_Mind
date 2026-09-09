resource "aws_ecr_repository" "application" {
  name                 = "${local.name}-application"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.data.arn
  }
}
resource "aws_ecs_cluster" "main" { name = local.name }
resource "aws_cloudwatch_log_group" "application" {
  name              = "/ecs/${local.name}"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.data.arn
}
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "ecs_secrets" {
  role   = aws_iam_role.ecs_execution.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "kms:Decrypt"], Resource = [aws_secretsmanager_secret.runtime.arn, aws_secretsmanager_secret.live_integrations.arn, aws_kms_key.data.arn] }] })
}
resource "aws_iam_role" "application_task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy" "application_task" {
  role = aws_iam_role.application_task.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${aws_s3_bucket.backups.arn}/*"] },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = [aws_kms_key.data.arn] }
  ] })
}
locals {
  image             = "${aws_ecr_repository.application.repository_url}:${var.application_image_tag}"
  log_configuration = { logDriver = "awslogs", options = { awslogs-group = aws_cloudwatch_log_group.application.name, awslogs-region = var.aws_region, awslogs-stream-prefix = "nivesh" } }
  common_environment = [
    { name = "NIVESH_ENV", value = "production" },
    { name = "NIVESH_LIVE_TRADING_ENABLED", value = "0" },
    { name = "LIVE_ELIGIBLE", value = "FALSE" },
    { name = "NIVESH_REQUIRE_SIGNED_MODEL", value = "1" },
    { name = "AWS_REGION", value = var.aws_region }
  ]
  runtime_secrets = [
    { name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:DATABASE_URL::" },
    { name = "REDIS_URL", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:REDIS_URL::" },
    { name = "NIVESH_SECRET", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:NIVESH_SECRET::" },
    { name = "NIVESH_MODEL_ARTIFACT_KEY", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:NIVESH_MODEL_ARTIFACT_KEY::" },
    { name = "NIVESH_BROKER_SESSION_KEY", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:NIVESH_BROKER_SESSION_KEY::" }
  ]
  integration_secrets = [
    { name = "KITE_API_KEY", valueFrom = "${aws_secretsmanager_secret.live_integrations.arn}:KITE_API_KEY::" },
    { name = "KITE_API_SECRET", valueFrom = "${aws_secretsmanager_secret.live_integrations.arn}:KITE_API_SECRET::" },
    { name = "KITE_ACCESS_TOKEN", valueFrom = "${aws_secretsmanager_secret.live_integrations.arn}:KITE_ACCESS_TOKEN::" },
    { name = "NIVESH_NEWS_API_KEY", valueFrom = "${aws_secretsmanager_secret.live_integrations.arn}:NIVESH_NEWS_API_KEY::" }
  ]
  common_secrets = concat(local.runtime_secrets, local.integration_secrets)
}
resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 4096
  memory                   = 16384
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.application_task.arn
  container_definitions    = jsonencode([{ name = "worker", image = local.image, essential = true, command = ["celery", "-A", "backend.tasks.worker:celery_app", "worker", "--loglevel=INFO", "--concurrency=1", "--prefetch-multiplier=1"], environment = local.common_environment, secrets = local.common_secrets, logConfiguration = local.log_configuration }])
}
resource "aws_ecs_task_definition" "beat" {
  family                   = "${local.name}-beat"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.application_task.arn
  container_definitions    = jsonencode([{ name = "beat", image = local.image, essential = true, command = ["celery", "-A", "backend.tasks.worker:celery_app", "beat", "--loglevel=INFO"], environment = local.common_environment, secrets = local.common_secrets, logConfiguration = local.log_configuration }])
}
resource "aws_ecs_task_definition" "stream" {
  family                   = "${local.name}-stream"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.application_task.arn
  container_definitions = jsonencode([{ name = "stream", image = local.image, essential = true,
    command = ["python", "-m", "backend.live_stream_service"], environment = local.common_environment,
  secrets = local.common_secrets, logConfiguration = local.log_configuration }])
}
resource "aws_ecs_task_definition" "web" {
  count                    = var.enable_web_ingress ? 1 : 0
  family                   = "${local.name}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.application_task.arn
  container_definitions = jsonencode([{ name = "web", image = local.image, essential = true,
    portMappings = [{ containerPort = 4173, protocol = "tcp" }],
    command      = ["uvicorn", "backend.asgi:app", "--host", "0.0.0.0", "--port", "4173", "--workers", "2", "--no-server-header", "--no-proxy-headers"],
    environment = [
      { name = "NIVESH_ENV", value = "production" }, { name = "NIVESH_COOKIE_SECURE", value = "1" },
      { name = "NIVESH_DEMO_MODE", value = "0" }, { name = "NIVESH_SIGNUP_ENABLED", value = "0" },
      { name = "NIVESH_ALLOWED_ORIGINS", value = var.public_origin }, { name = "NIVESH_ADMIN_EMAILS", value = var.administrator_emails },
      { name = "NIVESH_COGNITO_AUTH_REQUIRED", value = "1" }, { name = "NIVESH_TRUSTED_ALB_ARN", value = aws_lb.web[0].arn },
      { name = "NIVESH_COGNITO_LOGOUT_URL", value = "https://${aws_cognito_user_pool_domain.application[0].domain}.auth.${var.aws_region}.amazoncognito.com/logout?client_id=${aws_cognito_user_pool_client.application[0].id}&logout_uri=${urlencode(var.public_origin)}" },
      { name = "NIVESH_LIVE_TRADING_ENABLED", value = "0" }, { name = "LIVE_ELIGIBLE", value = "FALSE" },
      { name = "AWS_REGION", value = var.aws_region }
    ],
    secrets     = local.common_secrets, logConfiguration = local.log_configuration,
    healthCheck = { command = ["CMD-SHELL", "curl --fail --silent http://127.0.0.1:4173/api/health || exit 1"], interval = 30, timeout = 5, retries = 3, startPeriod = 60 }
  }])
}
resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.application.id]
    assign_public_ip = false
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
resource "aws_ecs_service" "beat" {
  name            = "beat"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.beat.arn
  desired_count   = var.beat_desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.application.id]
    assign_public_ip = false
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
resource "aws_ecs_service" "stream" {
  name            = "stream"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.stream.arn
  desired_count   = var.stream_desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.application.id]
    assign_public_ip = false
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
resource "aws_ecs_service" "web" {
  count           = var.enable_web_ingress ? 1 : 0
  name            = "web"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.web[0].arn
  desired_count   = var.web_desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.application.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.web[0].arn
    container_name   = "web"
    container_port   = 4173
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  lifecycle {
    precondition {
      condition     = var.web_desired_count >= 1
      error_message = "web_desired_count must be at least 1 when web ingress is enabled."
    }
  }
  depends_on = [aws_lb_listener.https]
}
resource "aws_ecs_task_definition" "restore" {
  family                   = "${local.name}-restore"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 2048
  memory                   = 8192
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.application_task.arn
  container_definitions = jsonencode([{ name = "restore", image = local.image, essential = true, command = ["python", "-m", "backend.remote_restore"], environment = concat(local.common_environment, [
    { name = "ALLOW_REMOTE_RESTORE", value = "YES" }, { name = "BACKUP_BUCKET", value = aws_s3_bucket.backups.bucket }, { name = "BACKUP_OBJECT_KEY", value = var.backup_object_key },
    { name = "BACKUP_SHA256", value = var.backup_sha256 }, { name = "EXPECTED_MARKET_BARS", value = "6435334" }, { name = "EXPECTED_FEATURE_ROWS", value = "3488676" }
  ]), secrets = [{ name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:DATABASE_URL::" }], logConfiguration = local.log_configuration }])
}

resource "aws_ecs_task_definition" "model_release" {
  family                   = "${local.name}-model-release"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 4096
  memory                   = 16384
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.application_task.arn
  container_definitions = jsonencode([{ name = "model-release", image = local.image, essential = true,
    command     = ["python", "-m", "backend.model_release", "retrain"],
    environment = concat(local.common_environment, [{ name = "MODEL_RELEASE_CONFIRM", value = "RETRAIN_AND_SIGN" }]),
    secrets     = local.runtime_secrets, logConfiguration = local.log_configuration
  }])
}

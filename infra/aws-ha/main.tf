locals {
  name = "nivesh-${var.environment}"
}

resource "random_password" "postgres" {
  length  = 40
  special = false
}
resource "random_password" "redis" {
  length  = 40
  special = false
}
resource "random_password" "application" {
  length  = 64
  special = false
}
resource "random_password" "model_artifact" {
  length  = 64
  special = false
}
resource "random_password" "broker_session" {
  length  = 64
  special = false
}
resource "aws_kms_key" "data" {
  description             = "${local.name} database encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}
resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}-data"
  target_key_id = aws_kms_key.data.key_id
}

resource "aws_security_group" "postgres" {
  name        = "${local.name}-postgres"
  description = "PostgreSQL from application only"
  vpc_id      = aws_vpc.main.id
  ingress {
    protocol        = "tcp"
    from_port       = 5432
    to_port         = 5432
    security_groups = [aws_security_group.application.id]
  }
  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "Redis TLS from application only"
  vpc_id      = aws_vpc.main.id
  ingress {
    protocol        = "tcp"
    from_port       = 6379
    to_port         = 6379
    security_groups = [aws_security_group.application.id]
  }
  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_subnet_group" "postgres" {
  name       = "${local.name}-postgres"
  subnet_ids = aws_subnet.private[*].id
}
resource "aws_db_parameter_group" "postgres" {
  name   = "${local.name}-postgres17"
  family = "postgres17"
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "immediate"
  }
  parameter {
    name         = "log_connections"
    value        = "1"
    apply_method = "immediate"
  }
  parameter {
    name         = "log_disconnections"
    value        = "1"
    apply_method = "immediate"
  }
  parameter {
    name         = "log_min_duration_statement"
    value        = "1000"
    apply_method = "immediate"
  }
}
resource "aws_db_instance" "postgres" {
  identifier                            = "${local.name}-postgres"
  engine                                = "postgres"
  engine_version                        = "17.5"
  instance_class                        = var.postgres_instance_class
  db_name                               = "nivesh"
  username                              = "nivesh_app"
  password                              = random_password.postgres.result
  port                                  = 5432
  allocated_storage                     = 100
  max_allocated_storage                 = 1000
  storage_type                          = "gp3"
  storage_encrypted                     = true
  kms_key_id                            = aws_kms_key.data.arn
  multi_az                              = true
  publicly_accessible                   = false
  db_subnet_group_name                  = aws_db_subnet_group.postgres.name
  vpc_security_group_ids                = [aws_security_group.postgres.id]
  parameter_group_name                  = aws_db_parameter_group.postgres.name
  backup_retention_period               = 35
  backup_window                         = "18:30-19:30"
  maintenance_window                    = "sun:19:30-sun:20:30"
  auto_minor_version_upgrade            = true
  deletion_protection                   = true
  skip_final_snapshot                   = false
  final_snapshot_identifier             = "${local.name}-postgres-final"
  performance_insights_enabled          = true
  performance_insights_retention_period = 7
  performance_insights_kms_key_id       = aws_kms_key.data.arn
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]
  copy_tags_to_snapshot                 = true
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.private[*].id
}
resource "aws_elasticache_parameter_group" "redis" {
  name   = "${local.name}-redis8"
  family = "redis8"
}
resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${local.name}-redis"
  description                = "Nivesh Celery broker and locks"
  node_type                  = var.redis_node_type
  port                       = 6379
  parameter_group_name       = aws_elasticache_parameter_group.redis.name
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  security_group_ids         = [aws_security_group.redis.id]
  num_cache_clusters         = 3
  automatic_failover_enabled = true
  multi_az_enabled           = true
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.redis.result
  kms_key_id                 = aws_kms_key.data.arn
  snapshot_retention_limit   = 7
  snapshot_window            = "18:00-19:00"
  maintenance_window         = "sun:20:30-sun:21:30"
  auto_minor_version_upgrade = true
  apply_immediately          = false
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_secretsmanager_secret" "runtime" {
  name                    = "${local.name}/runtime"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = 30
}
resource "aws_secretsmanager_secret_version" "runtime" {
  secret_id = aws_secretsmanager_secret.runtime.id
  secret_string = jsonencode({
    DATABASE_URL              = "postgresql+psycopg2://nivesh_app:${urlencode(random_password.postgres.result)}@${aws_db_instance.postgres.address}:5432/nivesh?sslmode=verify-full&sslrootcert=/etc/ssl/certs/rds-global-bundle.pem"
    REDIS_URL                 = "rediss://:${urlencode(random_password.redis.result)}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0?ssl_cert_reqs=required"
    NIVESH_SECRET             = random_password.application.result
    NIVESH_MODEL_ARTIFACT_KEY = random_password.model_artifact.result
    NIVESH_BROKER_SESSION_KEY = random_password.broker_session.result
  })
}

resource "aws_secretsmanager_secret" "live_integrations" {
  name                    = "${local.name}/live-integrations"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = 30
}
resource "aws_secretsmanager_secret_version" "live_integrations_bootstrap" {
  secret_id     = aws_secretsmanager_secret.live_integrations.id
  secret_string = jsonencode({ LIVE_ELIGIBLE = "FALSE" })
  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_cloudwatch_metric_alarm" "postgres_cpu" {
  alarm_name          = "${local.name}-postgres-high-cpu"
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.postgres.id }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
resource "aws_cloudwatch_metric_alarm" "postgres_storage" {
  alarm_name          = "${local.name}-postgres-low-storage"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10737418240
  comparison_operator = "LessThanThreshold"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.postgres.id }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
resource "aws_cloudwatch_metric_alarm" "redis_memory" {
  alarm_name          = "${local.name}-redis-high-memory"
  namespace           = "AWS/ElastiCache"
  metric_name         = "DatabaseMemoryUsagePercentage"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  dimensions          = { ReplicationGroupId = aws_elasticache_replication_group.redis.id }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

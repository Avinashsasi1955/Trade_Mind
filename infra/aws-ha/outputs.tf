output "postgres_endpoint" {
  value = aws_db_instance.postgres.address
}
output "redis_primary_endpoint" {
  value = aws_elasticache_replication_group.redis.primary_endpoint_address
}
output "runtime_secret_arn" {
  value = aws_secretsmanager_secret.runtime.arn
}
output "live_integrations_secret_arn" {
  value = aws_secretsmanager_secret.live_integrations.arn
}
output "postgres_security_group_id" {
  value = aws_security_group.postgres.id
}
output "redis_security_group_id" {
  value = aws_security_group.redis.id
}
output "vpc_id" {
  value = aws_vpc.main.id
}
output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}
output "application_security_group_id" {
  value = aws_security_group.application.id
}
output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
output "backup_bucket" { value = aws_s3_bucket.backups.bucket }
output "backup_kms_key_arn" { value = aws_kms_key.data.arn }
output "ecr_repository_url" { value = aws_ecr_repository.application.repository_url }
output "ecs_cluster_name" { value = aws_ecs_cluster.main.name }
output "restore_task_definition_arn" { value = aws_ecs_task_definition.restore.arn }
output "model_release_task_definition_arn" { value = aws_ecs_task_definition.model_release.arn }
output "web_load_balancer_dns" { value = var.enable_web_ingress ? aws_lb.web[0].dns_name : null }
output "web_waf_arn" { value = var.enable_web_ingress ? aws_wafv2_web_acl.web[0].arn : null }
output "cognito_user_pool_id" { value = var.enable_web_ingress ? aws_cognito_user_pool.application[0].id : null }
output "cognito_managed_login_domain" { value = var.enable_web_ingress ? "https://${aws_cognito_user_pool_domain.application[0].domain}.auth.${var.aws_region}.amazoncognito.com" : null }

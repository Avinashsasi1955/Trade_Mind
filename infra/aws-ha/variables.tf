variable "aws_region" {
  type    = string
  default = "ap-south-1"
}
variable "environment" {
  type    = string
  default = "production"
}
variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}
variable "postgres_instance_class" {
  type    = string
  default = "db.r7g.large"
}
variable "redis_node_type" {
  type    = string
  default = "cache.r7g.large"
}
variable "application_image_tag" {
  type    = string
  default = "v3.9"
}
variable "enable_web_ingress" {
  type        = bool
  default     = false
  description = "Creates billable ALB/WAF resources only after explicit operator approval."
}
variable "web_desired_count" {
  type    = number
  default = 0
  validation {
    condition     = var.web_desired_count >= 0 && var.web_desired_count <= 4
    error_message = "Web count must be 0–4."
  }
}
variable "public_origin" {
  type        = string
  default     = ""
  description = "Exact HTTPS browser origin, for example https://trade.example.com."
  validation {
    condition     = !var.enable_web_ingress || startswith(var.public_origin, "https://")
    error_message = "An HTTPS public_origin is required when web ingress is enabled."
  }
}
variable "administrator_emails" {
  type        = string
  default     = ""
  description = "Comma-separated application administrator email allow-list."
  validation {
    condition     = !var.enable_web_ingress || length(trimspace(var.administrator_emails)) > 3
    error_message = "administrator_emails is required when web ingress is enabled."
  }
}
variable "acm_certificate_arn" {
  type        = string
  default     = ""
  description = "Validated ACM certificate ARN for public_origin."
  validation {
    condition     = !var.enable_web_ingress || startswith(var.acm_certificate_arn, "arn:aws:acm:")
    error_message = "A valid ACM certificate ARN is required when web ingress is enabled."
  }
}
variable "cognito_domain_prefix" {
  type        = string
  default     = ""
  description = "Globally unique Cognito managed-login prefix."
  validation {
    condition     = !var.enable_web_ingress || can(regex("^[a-z0-9-]{8,63}$", var.cognito_domain_prefix))
    error_message = "A lowercase 8–63 character Cognito domain prefix is required when web ingress is enabled."
  }
}
variable "worker_desired_count" {
  type    = number
  default = 0
  validation {
    condition     = var.worker_desired_count >= 0 && var.worker_desired_count <= 4
    error_message = "Worker count must be 0–4."
  }
}
variable "beat_desired_count" {
  type    = number
  default = 0
  validation {
    condition     = contains([0, 1], var.beat_desired_count)
    error_message = "Beat count must be exactly 0 or 1."
  }
}
variable "stream_desired_count" {
  type    = number
  default = 0
  validation {
    condition     = contains([0, 1], var.stream_desired_count)
    error_message = "Stream service count must be exactly 0 or 1."
  }
}
variable "backup_object_key" {
  type    = string
  default = "baseline/postgres-v3.4.dump"
}
variable "backup_sha256" {
  type    = string
  default = "aa7d88e60e500040fc0c9d7e0d2ba59e54ef3044e49874b72412a6c7665a7ce4"
}

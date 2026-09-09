resource "aws_security_group" "load_balancer" {
  count       = var.enable_web_ingress ? 1 : 0
  name        = "${local.name}-alb"
  description = "Public HTTPS ingress to the application load balancer"
  vpc_id      = aws_vpc.main.id
  ingress {
    protocol    = "tcp"
    from_port   = 443
    to_port     = 443
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    protocol    = "tcp"
    from_port   = 80
    to_port     = 80
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    protocol        = "tcp"
    from_port       = 4173
    to_port         = 4173
    security_groups = [aws_security_group.application.id]
  }
}

resource "aws_security_group_rule" "web_from_alb" {
  count                    = var.enable_web_ingress ? 1 : 0
  type                     = "ingress"
  protocol                 = "tcp"
  from_port                = 4173
  to_port                  = 4173
  source_security_group_id = aws_security_group.load_balancer[0].id
  security_group_id        = aws_security_group.application.id
}

resource "aws_lb" "web" {
  count                      = var.enable_web_ingress ? 1 : 0
  name                       = substr("${local.name}-web", 0, 32)
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.load_balancer[0].id]
  subnets                    = aws_subnet.public[*].id
  drop_invalid_header_fields = true
  enable_deletion_protection = true
  access_logs {
    bucket  = aws_s3_bucket.alb_logs[0].bucket
    enabled = true
  }
}

resource "aws_s3_bucket" "alb_logs" {
  count         = var.enable_web_ingress ? 1 : 0
  bucket_prefix = "${local.name}-alb-logs-"
  force_destroy = false
}
resource "aws_s3_bucket_public_access_block" "alb_logs" {
  count                   = var.enable_web_ingress ? 1 : 0
  bucket                  = aws_s3_bucket.alb_logs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  count  = var.enable_web_ingress ? 1 : 0
  bucket = aws_s3_bucket.alb_logs[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
data "aws_caller_identity" "current" {}
resource "aws_s3_bucket_policy" "alb_logs" {
  count  = var.enable_web_ingress ? 1 : 0
  bucket = aws_s3_bucket.alb_logs[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Principal = { Service = "logdelivery.elasticloadbalancing.amazonaws.com" }, Action = "s3:PutObject",
    Resource = "${aws_s3_bucket.alb_logs[0].arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
  }] })
}

resource "aws_lb_target_group" "web" {
  count       = var.enable_web_ingress ? 1 : 0
  name_prefix = "nwv3-"
  port        = 4173
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id
  health_check {
    path                = "/api/health"
    matcher             = "200"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
  }
}
resource "aws_lb_listener" "http" {
  count             = var.enable_web_ingress ? 1 : 0
  load_balancer_arn = aws_lb.web[0].arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
resource "aws_lb_listener" "https" {
  count             = var.enable_web_ingress ? 1 : 0
  load_balancer_arn = aws_lb.web[0].arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn
  default_action {
    type  = "authenticate-cognito"
    order = 1
    authenticate_cognito {
      user_pool_arn              = aws_cognito_user_pool.application[0].arn
      user_pool_client_id        = aws_cognito_user_pool_client.application[0].id
      user_pool_domain           = aws_cognito_user_pool_domain.application[0].domain
      on_unauthenticated_request = "authenticate"
      scope                      = "openid email profile"
      session_cookie_name        = "AWSELBAuthSessionCookie"
      session_timeout            = 3600
    }
  }
  default_action {
    type             = "forward"
    order            = 2
    target_group_arn = aws_lb_target_group.web[0].arn
  }
}

resource "aws_wafv2_web_acl" "web" {
  count = var.enable_web_ingress ? 1 : 0
  name  = "${local.name}-web"
  scope = "REGIONAL"
  default_action {
    allow {}
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-web"
    sampled_requests_enabled   = true
  }
  rule {
    name     = "AWSCommonRules"
    priority = 10
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "common"
      sampled_requests_enabled   = true
    }
  }
  rule {
    name     = "AWSKnownBadInputs"
    priority = 20
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bad-inputs"
      sampled_requests_enabled   = true
    }
  }
  rule {
    name     = "AWSIPReputation"
    priority = 30
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "ip-reputation"
      sampled_requests_enabled   = true
    }
  }
  rule {
    name     = "RateLimit"
    priority = 40
    action {
      block {}
    }
    statement {
      rate_based_statement {
        aggregate_key_type = "IP"
        limit              = 1000
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "rate-limit"
      sampled_requests_enabled   = true
    }
  }
}
resource "aws_wafv2_web_acl_association" "web" {
  count        = var.enable_web_ingress ? 1 : 0
  resource_arn = aws_lb.web[0].arn
  web_acl_arn  = aws_wafv2_web_acl.web[0].arn
}

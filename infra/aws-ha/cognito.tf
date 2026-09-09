resource "aws_cognito_user_pool" "application" {
  count                    = var.enable_web_ingress ? 1 : 0
  name                     = "${local.name}-users"
  deletion_protection      = "ACTIVE"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "ON"

  software_token_mfa_configuration {
    enabled = true
  }
  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    require_uppercase                = true
    temporary_password_validity_days = 2
  }
  admin_create_user_config {
    allow_admin_create_user_only = true
  }
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
  user_attribute_update_settings {
    attributes_require_verification_before_update = ["email"]
  }
  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cognito_user_pool_client" "application" {
  count                                = var.enable_web_ingress ? 1 : 0
  name                                 = "${local.name}-web"
  user_pool_id                         = aws_cognito_user_pool.application[0].id
  generate_secret                      = true
  prevent_user_existence_errors        = "ENABLED"
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${var.public_origin}/oauth2/idpresponse"]
  logout_urls                          = [var.public_origin]
  access_token_validity                = 60
  id_token_validity                    = 60
  refresh_token_validity               = 1
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
  enable_token_revocation = true
}

resource "aws_cognito_user_pool_domain" "application" {
  count        = var.enable_web_ingress ? 1 : 0
  domain       = var.cognito_domain_prefix
  user_pool_id = aws_cognito_user_pool.application[0].id
}

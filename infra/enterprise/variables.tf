variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "region" {
  type        = string
  default     = "europe-west3"
  description = "Target GCP region (pinned for Pub/Sub, Firestore, Cloud Tasks, Cloud Run)."
}

variable "name_prefix" {
  type        = string
  default     = "looker-jit"
}

variable "container_image" {
  type        = string
  description = "Artifact Registry image URI built from backend/Dockerfile."
}

variable "looker_private_base_url" {
  type        = string
  description = "Private IP / PSC Internal ALB HTTPS URL of Looker Core (no trailing slash)."
}

variable "iga_base_url" {
  type        = string
  description = "Internal HTTPS base URL of Enterprise IGA / HR entitlement facade."
}

variable "vpc_network" {
  type        = string
  description = "VPC network name or full Shared VPC path for Direct VPC Egress."
}

variable "vpc_subnetwork" {
  type        = string
  description = "Subnetwork name or full Shared VPC path with connectivity to Looker PSC ALB & IGA."
}

variable "looker_login_log_name" {
  type        = string
  default     = "looker.googleapis.com%2Fuser_login"
  description = "URL-encoded Cloud Logging logName verified in Phase 0 (user_login or UserLogin)."
}

variable "provisioning_sa_looker_user_id" {
  type        = string
  default     = ""
  description = "Numeric Looker user ID of the provisioning API Service Account (excluded to prevent loops)."
}

variable "pending_marker_group" {
  type        = string
  default     = "BI_Access_Pending_Approval"
  description = "Zero-privilege Looker group assigned when IGA returns no entitlements."
}

variable "managed_group_prefix" {
  type        = string
  default     = "BI_"
  description = "Prefix scoping safe delta revocation (never touches All Users or SAML mirrored groups)."
}

variable "firestore_database_name" {
  type        = string
  default     = "(default)"
}

variable "create_firestore_database" {
  type        = bool
  default     = false
  description = "Set true to create a new Firestore Native database; false to reuse existing."
}

variable "kms_key_name" {
  type        = string
  default     = ""
  description = "Optional regional Cloud KMS key resource ID for CMEK encryption."
}

variable "high_queue_dispatches_per_second" {
  type        = number
  default     = 6
  description = "Live onboarding dispatch cap (~15-20 Looker API calls/s with 2-3 calls/user)."
}

variable "high_queue_max_concurrent" {
  type        = number
  default     = 10
}

variable "low_queue_dispatches_per_second" {
  type        = number
  default     = 2
  description = "Background delta revocation dispatch cap (isolated from live logins)."
}

variable "low_queue_max_concurrent" {
  type        = number
  default     = 2
}

variable "worker_max_instances" {
  type        = number
  default     = 4
}

variable "reconcile_schedule_cron" {
  type        = string
  default     = "*/30 * * * *"
  description = "Cron schedule for polling the Enterprise IGA change feed."
}

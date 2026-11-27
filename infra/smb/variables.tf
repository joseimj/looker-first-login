variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "region" {
  type        = string
  default     = "europe-west3"
  description = "Target GCP region."
}

variable "name_prefix" {
  type        = string
  default     = "looker-jit"
  description = "Prefix for created resource names."
}

variable "container_image" {
  type        = string
  description = "Artifact Registry image URI built from backend/Dockerfile."
}

variable "looker_base_url" {
  type        = string
  description = "Looker (Google Cloud core) HTTPS URL (no trailing slash)."
}

variable "use_group_ranked_landing_page" {
  type        = bool
  default     = true
  description = "When true, skips per-user landing_page writes to save 1 Admin API call/user."
}

variable "require_saml" {
  type        = bool
  default     = true
  description = "When true, only accounts with active SAML credentials are provisioned."
}

variable "max_instances" {
  type        = number
  default     = 2
  description = "Upper bound on Cloud Run replicas to protect Looker API concurrency."
}

variable "looker_max_concurrency" {
  type        = number
  default     = 10
  description = "Max simultaneous in-flight Looker API requests per container."
}

# ENTERPRISE PROFILE: Converged 3-Plane Private Event-Driven Topology
#
# Topology:
#   Looker Core (Private IP / PSC · Zero-Egress Passive Polling Extension)
#     -> Cloud Logging (`user_login`) -> Log Router Sink (structured filter, excludes SA ID)
#     -> Pub/Sub (Regional + optional CMEK + 7d Dead-Letter Topic)
#     -> Cloud Run Gate (Zero-PII Firestore Ledger ~15ms dedupe)
#     -> Dual Priority Cloud Tasks Queues:
#          * jit-onboarding-high (live Day-1 logins)
#          * reconcile-low       (Cloud Scheduler IGA change-feed delta revocations)
#     -> Cloud Run Worker (Direct VPC Egress -> PSC Northbound Internal ALB -> Looker API 4.0)
#
# Note: Run `terraform validate` and `terraform plan` in your target project
# after populating terraform.tfvars and creating Secret Manager secret versions.

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ---- 1. CAPTURE & BUFFER (Cloud Logging Sink -> Pub/Sub + DLT) ----

resource "google_pubsub_topic" "login_events" {
  name         = "${var.name_prefix}-user-logins"
  kms_key_name = var.kms_key_name != "" ? var.kms_key_name : null

  message_storage_policy {
    allowed_persistence_regions = [var.region]
  }
}

resource "google_pubsub_topic" "login_dlt" {
  name         = "${var.name_prefix}-user-logins-dlt"
  kms_key_name = var.kms_key_name != "" ? var.kms_key_name : null

  message_storage_policy {
    allowed_persistence_regions = [var.region]
  }
}

resource "google_pubsub_subscription" "dlt_pull_replay" {
  name                       = "${var.name_prefix}-user-logins-dlt-replay"
  topic                      = google_pubsub_topic.login_dlt.id
  message_retention_duration = "604800s" # 7-day replay window
}

# Structured filter — NO brittle free-text SSO regex! Excludes the Provisioning
# Service Account actor.id deterministically to prevent feedback loops.
# Verify exact `var.looker_login_log_name` in Logs Explorer during Phase 0.
resource "google_logging_project_sink" "looker_user_login" {
  name                   = "${var.name_prefix}-user-login-sink"
  destination            = "pubsub.googleapis.com/${google_pubsub_topic.login_events.id}"
  unique_writer_identity = true
  filter                 = <<-EOT
    resource.type="looker.googleapis.com/Instance"
    AND logName="projects/${var.project_id}/logs/${var.looker_login_log_name}"
    AND jsonPayload.actor.id!=""
    ${var.provisioning_sa_looker_user_id != "" ? "AND jsonPayload.actor.id!=\"${var.provisioning_sa_looker_user_id}\"" : ""}
  EOT
}

# ---- 2. STATE (Zero-PII Regional Firestore Ledger) ----

resource "google_firestore_database" "ledger" {
  count       = var.create_firestore_database ? 1 : 0
  project     = var.project_id
  name        = var.firestore_database_name
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
}

# ---- 3. THROTTLE (Dual Priority Cloud Tasks Queues) ----

resource "google_cloud_tasks_queue" "jit_high" {
  name     = "${var.name_prefix}-onboarding-high"
  location = var.region

  rate_limits {
    # Paced in tasks/sec so total Looker API calls/sec stay well within 20-25/s.
    max_dispatches_per_second = var.high_queue_dispatches_per_second
    max_concurrent_dispatches = var.high_queue_max_concurrent
  }
  retry_config {
    max_attempts = 6
    min_backoff  = "2s"
    max_backoff  = "60s"
  }
}

resource "google_cloud_tasks_queue" "reconcile_low" {
  name     = "${var.name_prefix}-reconcile-low"
  location = var.region

  rate_limits {
    # Background revocations are strictly isolated from live Day-1 logins.
    max_dispatches_per_second = var.low_queue_dispatches_per_second
    max_concurrent_dispatches = var.low_queue_max_concurrent
  }
  retry_config {
    max_attempts = 5
    min_backoff  = "5s"
    max_backoff  = "120s"
  }
}

# ---- 4. SECRETS (Looker API3 + IGA Token) ----

resource "google_secret_manager_secret" "looker_client_id" {
  secret_id = "${var.name_prefix}-looker-client-id"
  replication {
    user_managed {
      replicas {
        location = var.region
        dynamic "customer_managed_encryption" {
          for_each = var.kms_key_name != "" ? [1] : []
          content { kms_key_name = var.kms_key_name }
        }
      }
    }
  }
}

resource "google_secret_manager_secret" "looker_client_secret" {
  secret_id = "${var.name_prefix}-looker-client-secret"
  replication {
    user_managed {
      replicas {
        location = var.region
        dynamic "customer_managed_encryption" {
          for_each = var.kms_key_name != "" ? [1] : []
          content { kms_key_name = var.kms_key_name }
        }
      }
    }
  }
}

resource "google_secret_manager_secret" "iga_bearer_token" {
  secret_id = "${var.name_prefix}-iga-bearer-token"
  replication {
    user_managed {
      replicas {
        location = var.region
        dynamic "customer_managed_encryption" {
          for_each = var.kms_key_name != "" ? [1] : []
          content { kms_key_name = var.kms_key_name }
        }
      }
    }
  }
}

# ---- 5. API WRITER (Cloud Run Worker w/ Direct VPC Egress -> PSC Northbound) ----

resource "google_cloud_run_v2_service" "worker" {
  name                = "${var.name_prefix}-worker"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = false

  template {
    service_account = google_service_account.worker.email
    scaling {
      min_instance_count = 0
      max_instance_count = var.worker_max_instances
    }
    max_instance_request_concurrency = 4

    vpc_access {
      network_interfaces {
        network    = var.vpc_network
        subnetwork = var.vpc_subnetwork
      }
      egress = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = var.container_image

      env {
        name  = "DEPLOYMENT_PROFILE"
        value = "worker"
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = var.firestore_database_name
      }
      env {
        name  = "LOOKER_BASE_URL"
        value = var.looker_private_base_url
      }
      env {
        name  = "IGA_BASE_URL"
        value = var.iga_base_url
      }
      env {
        name  = "NO_LANDING_PAGE"
        value = "true"
      }
      env {
        name  = "REQUIRE_SAML"
        value = "true"
      }
      env {
        name  = "PENDING_MARKER_GROUP"
        value = var.pending_marker_group
      }
      env {
        name  = "MANAGED_GROUP_PREFIX"
        value = var.managed_group_prefix
      }
      env {
        name  = "LOOKER_MAX_CONCURRENCY"
        value = "4"
      }
      env {
        name = "LOOKER_CLIENT_ID"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.looker_client_id.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "LOOKER_CLIENT_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.looker_client_secret.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "IGA_BEARER_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.iga_bearer_token.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.worker_secrets]
}

# ---- 6. GATE & DELTA RECONCILER (Cloud Run Gate + Pub/Sub Push + Scheduler) ----

resource "google_cloud_run_v2_service" "gate" {
  name                = "${var.name_prefix}-gate"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = false

  template {
    service_account = google_service_account.gate.email
    scaling {
      min_instance_count = 0
      max_instance_count = 10
    }
    max_instance_request_concurrency = 40

    # Direct VPC egress allows /tasks/reconcile to query private IGA + Looker
    # search_users_by_email when triggered by Cloud Scheduler.
    vpc_access {
      network_interfaces {
        network    = var.vpc_network
        subnetwork = var.vpc_subnetwork
      }
      egress = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = var.container_image

      env {
        name  = "DEPLOYMENT_PROFILE"
        value = "gate"
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = var.firestore_database_name
      }
      env {
        name  = "LOOKER_BASE_URL"
        value = var.looker_private_base_url
      }
      env {
        name  = "IGA_BASE_URL"
        value = var.iga_base_url
      }
      env {
        name  = "EXCLUDED_ACTOR_IDS"
        value = var.provisioning_sa_looker_user_id
      }
      env {
        name  = "CLOUD_TASKS_LOCATION"
        value = var.region
      }
      env {
        name  = "CLOUD_TASKS_HIGH_QUEUE"
        value = google_cloud_tasks_queue.jit_high.name
      }
      env {
        name  = "CLOUD_TASKS_LOW_QUEUE"
        value = google_cloud_tasks_queue.reconcile_low.name
      }
      env {
        name  = "CLOUD_TASKS_WORKER_URL"
        value = "${google_cloud_run_v2_service.worker.uri}/tasks/provision"
      }
      env {
        name  = "CLOUD_TASKS_OIDC_SA"
        value = google_service_account.tasks_invoker.email
      }
      env {
        name  = "CLOUD_TASKS_OIDC_AUDIENCE"
        value = google_cloud_run_v2_service.worker.uri
      }
      env {
        name = "LOOKER_CLIENT_ID"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.looker_client_id.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "LOOKER_CLIENT_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.looker_client_secret.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "IGA_BEARER_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.iga_bearer_token.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.gate_secrets]
}

resource "google_pubsub_subscription" "gate_push" {
  name                 = "${var.name_prefix}-gate-push"
  topic                = google_pubsub_topic.login_events.id
  ack_deadline_seconds = 30

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.gate.uri}/events/user-login"
    oidc_token {
      service_account_email = google_service_account.pubsub_invoker.email
      audience              = google_cloud_run_v2_service.gate.uri
    }
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.login_dlt.id
    max_delivery_attempts = 5
  }

  retry_policy {
    minimum_backoff = "5s"
    maximum_backoff = "60s"
  }
}

resource "google_cloud_scheduler_job" "delta_reconcile" {
  name        = "${var.name_prefix}-delta-reconcile"
  region      = var.region
  description = "Polls Enterprise IGA change feed and enqueues low-priority delta revocations."
  schedule    = var.reconcile_schedule_cron
  time_zone   = "UTC"

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.gate.uri}/tasks/reconcile"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oidc_token {
      service_account_email = google_service_account.scheduler_invoker.email
      audience              = google_cloud_run_v2_service.gate.uri
    }
  }
}

# SMB / Mid-Market Profile: Single Cloud Run Service + Secret Manager
#
# Lightweight footprint (~50 to 1,000 users, Public IP Looker Core).
# Extension calls POST /provision via Looker serverProxy using a shared secret
# held in a hidden Looker scoped user attribute (never exposed to browsers).

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

resource "google_service_account" "provisioner" {
  account_id   = "${var.name_prefix}-smb-sa"
  display_name = "Looker Core JIT Provisioner (SMB)"
}

resource "google_secret_manager_secret" "looker_client_id" {
  secret_id = "${var.name_prefix}-looker-client-id"
  replication {
    user_managed {
      replicas { location = var.region }
    }
  }
}

resource "google_secret_manager_secret" "looker_client_secret" {
  secret_id = "${var.name_prefix}-looker-client-secret"
  replication {
    user_managed {
      replicas { location = var.region }
    }
  }
}

resource "google_secret_manager_secret" "shared_secret" {
  secret_id = "${var.name_prefix}-shared-secret"
  replication {
    user_managed {
      replicas { location = var.region }
    }
  }
}

resource "google_secret_manager_secret_iam_member" "sa_secret_access" {
  for_each = {
    client_id     = google_secret_manager_secret.looker_client_id.secret_id
    client_secret = google_secret_manager_secret.looker_client_secret.secret_id
    shared_secret = google_secret_manager_secret.shared_secret.secret_id
  }
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.provisioner.email}"
}

resource "google_cloud_run_v2_service" "provisioner" {
  name                = "${var.name_prefix}-smb"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.provisioner.email
    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }
    max_instance_request_concurrency = 10

    containers {
      image = var.container_image

      env {
        name  = "DEPLOYMENT_PROFILE"
        value = "smb"
      }
      env {
        name  = "LOOKER_BASE_URL"
        value = var.looker_base_url
      }
      env {
        name  = "ALLOWED_ORIGINS"
        value = var.looker_base_url
      }
      env {
        name  = "NO_LANDING_PAGE"
        value = var.use_group_ranked_landing_page ? "true" : "false"
      }
      env {
        name  = "REQUIRE_SAML"
        value = var.require_saml ? "true" : "false"
      }
      env {
        name  = "LOOKER_MAX_CONCURRENCY"
        value = tostring(var.looker_max_concurrency)
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
        name = "PROVISIONING_SHARED_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.shared_secret.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.sa_secret_access]
}

# Looker serverProxy authenticates with the hidden PROVISIONING_SHARED_SECRET
# (injected via createSecretKeyTag), so Cloud Run allows public HTTPS ingress
# and enforces HMAC shared-secret authentication at the application layer.
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  location = google_cloud_run_v2_service.provisioner.location
  name     = google_cloud_run_v2_service.provisioner.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

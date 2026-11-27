# Least-privilege IAM bindings for the Enterprise topology.

data "google_project" "current" {
  project_id = var.project_id
}

resource "google_service_account" "gate" {
  account_id   = "${var.name_prefix}-gate-sa"
  display_name = "Looker JIT Gate Service Account"
}

resource "google_service_account" "worker" {
  account_id   = "${var.name_prefix}-worker-sa"
  display_name = "Looker JIT Worker Service Account"
}

resource "google_service_account" "pubsub_invoker" {
  account_id   = "${var.name_prefix}-pubsub-inv-sa"
  display_name = "Pub/Sub Push -> Gate Invoker SA"
}

resource "google_service_account" "tasks_invoker" {
  account_id   = "${var.name_prefix}-tasks-inv-sa"
  display_name = "Cloud Tasks -> Worker Invoker SA"
}

resource "google_service_account" "scheduler_invoker" {
  account_id   = "${var.name_prefix}-sched-inv-sa"
  display_name = "Cloud Scheduler -> Gate Reconcile Invoker SA"
}

# Log Router Sink -> Pub/Sub publisher.
resource "google_pubsub_topic_iam_member" "sink_publisher" {
  topic  = google_pubsub_topic.login_events.id
  role   = "roles/pubsub.publisher"
  member = google_logging_project_sink.looker_user_login.writer_identity
}

# Pub/Sub Service Agent -> Dead-Letter Topic forwarding permissions.
resource "google_pubsub_topic_iam_member" "dlt_publisher" {
  topic  = google_pubsub_topic.login_dlt.id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription_iam_member" "dlt_subscriber" {
  subscription = google_pubsub_subscription.gate_push.id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# Pub/Sub push & Cloud Scheduler -> invoke Cloud Run Gate (IAM authenticated).
resource "google_cloud_run_v2_service_iam_member" "pubsub_invoke_gate" {
  location = google_cloud_run_v2_service.gate.location
  name     = google_cloud_run_v2_service.gate.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.pubsub_invoker.email}"
}

resource "google_cloud_run_v2_service_iam_member" "scheduler_invoke_gate" {
  location = google_cloud_run_v2_service.gate.location
  name     = google_cloud_run_v2_service.gate.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

# Cloud Tasks -> invoke Cloud Run Worker (IAM authenticated).
resource "google_cloud_run_v2_service_iam_member" "tasks_invoke_worker" {
  location = google_cloud_run_v2_service.worker.location
  name     = google_cloud_run_v2_service.worker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tasks_invoker.email}"
}

# Gate -> enqueue Cloud Tasks + actAs tasks_invoker SA to mint OIDC tokens.
resource "google_project_iam_member" "gate_tasks_enqueuer" {
  project = var.project_id
  role    = "roles/cloudtasks.enqueuer"
  member  = "serviceAccount:${google_service_account.gate.email}"
}

resource "google_service_account_iam_member" "gate_act_as_tasks_invoker" {
  service_account_id = google_service_account.tasks_invoker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.gate.email}"
}

# Gate & Worker -> Firestore zero-PII ledger access.
resource "google_project_iam_member" "gate_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.gate.email}"
}

resource "google_project_iam_member" "worker_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

# Gate & Worker -> Secret Manager access for Looker API3 & IGA credentials.
resource "google_secret_manager_secret_iam_member" "worker_secrets" {
  for_each = {
    client_id     = google_secret_manager_secret.looker_client_id.secret_id
    client_secret = google_secret_manager_secret.looker_client_secret.secret_id
    iga_token     = google_secret_manager_secret.iga_bearer_token.secret_id
  }
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "gate_secrets" {
  for_each = {
    client_id     = google_secret_manager_secret.looker_client_id.secret_id
    client_secret = google_secret_manager_secret.looker_client_secret.secret_id
    iga_token     = google_secret_manager_secret.iga_bearer_token.secret_id
  }
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gate.email}"
}

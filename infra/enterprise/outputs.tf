output "gate_service_uri" {
  value = google_cloud_run_v2_service.gate.uri
}

output "worker_service_uri" {
  value = google_cloud_run_v2_service.worker.uri
}

output "log_sink_writer_identity" {
  value = google_logging_project_sink.looker_user_login.writer_identity
}

output "high_priority_queue" {
  value = google_cloud_tasks_queue.jit_high.id
}

output "low_priority_queue" {
  value = google_cloud_tasks_queue.reconcile_low.id
}

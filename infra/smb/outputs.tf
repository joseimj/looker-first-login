output "provision_endpoint_url" {
  value       = "${google_cloud_run_v2_service.provisioner.uri}/provision"
  description = "Set this as backendUrl in extension/src/config.js (mode='direct', transport='server')."
}

output "external_api_origin" {
  value       = google_cloud_run_v2_service.provisioner.uri
  description = "Add this origin to external_api_urls in lookml/manifest.smb.lkml."
}

project_name: "provisioning_landing"

# ENTERPRISE PROFILE (Zero-Egress Passive Polling)
# Copy this file to `manifest.lkml` in your LookML project.
#
# Security highlight: `external_api_urls: []` — Looker Core makes ZERO outbound
# calls from the extension. No CORS, no shared secrets, no PSC southbound LB.
# Provisioning is triggered privately inside GCP from Cloud Logging `user_login`.
application: splash {
  label: "Preparing your workspace"
  file: "bundle.js"
  # url: "http://localhost:8080/bundle.js"  # Uncomment for local Dev Mode only.

  mount_points: {
    # Keep `yes` during PoC testing; set `no` in production to hide from the
    # Applications menu (verify direct URL routing before switching to `no`).
    standalone: yes
  }

  entitlements: {
    navigation: yes
    new_window: yes
    # Only if `accessRequestUrl` is configured in extension/src/config.js:
    # new_window_external_urls: ["https://iga.corp.example/*"]
    core_api_methods: ["me"]
    external_api_urls: []
  }
}

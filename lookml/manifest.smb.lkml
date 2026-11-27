project_name: "provisioning_landing"

# SMB / FAST POC PROFILE (Direct Trigger)
# Copy this file to `manifest.lkml` in your LookML project and set
# `mode: 'direct'` in extension/src/config.js.
application: splash {
  label: "Preparing your workspace"
  file: "bundle.js"
  # url: "http://localhost:8080/bundle.js"  # Uncomment for local Dev Mode only.

  mount_points: {
    standalone: yes
  }

  entitlements: {
    navigation: yes
    new_window: yes
    core_api_methods: ["me"]
    # Must match `backendUrl` in extension/src/config.js.
    # For Cloud Run (Stage 2), replace localhost with your HTTPS service URL.
    external_api_urls: ["http://localhost:8787"]
    # Stage 2 (`transport: 'server'` + secret key tag): uncomment and create a
    # hidden user attribute named `provisioning_landing_splash_backend_secret`
    # with User Access = None (or View), Hide Values = Yes, and Domain Allowlist
    # set to your backend HTTPS URL.
    # scoped_user_attributes: ["backend_secret"]
  }
}

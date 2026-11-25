project_name: "provisioning_landing"

# Landing extension shown to first-time users while their Looker groups and
# user attributes are provisioned via API.
#
#   Extension ID: provisioning_landing::splash
#   URL:          /extensions/provisioning_landing::splash
#   Spartan URL:  /spartan/provisioning_landing::splash  (optional, hides nav)
#
# Production: build extension/dist/bundle.js (`npm run build`), upload it to
# the root of this LookML project, and keep `file: "bundle.js"` below.
# Development: comment out `file:` and uncomment `url:` while running
# `npm run develop` in extension/.
application: splash {
  label: "Preparing your workspace"
  file: "bundle.js"
  # url: "http://localhost:8080/bundle.js"

  mount_points: {
    # Set to `yes` during the PoC so the extension appears in the Applications
    # menu for easy testing. In production you may switch this to `no` to hide
    # it from the Applications menu (verify direct URL access still works on
    # your Looker version before rolling out).
    standalone: yes
  }

  entitlements: {
    # Redirect to the user's dashboards once access is granted.
    navigation: yes
    # Needed if the Looker host treats `_self` or `_blank` redirects as opening
    # a window. Remove if your verified redirectMode works without it.
    new_window: yes
    # The extension only reads the Looker ID of the signed-in user.
    core_api_methods: ["me"]
    # Provisioning backend origins. Must match `backendUrl` in
    # extension/src/config.js (replace localhost with your Stage 2 HTTPS URL).
    external_api_urls: ["http://localhost:8787"]
    # Stage 2 (serverProxy + secret key tag) only: uncomment and create a
    # hidden user attribute named `provisioning_landing_splash_backend_secret`
    # with User Access = None (or View), Hide Values = Yes, and Domain
    # Allowlist set to your backend HTTPS URL.
    # scoped_user_attributes: ["backend_secret"]
  }
}

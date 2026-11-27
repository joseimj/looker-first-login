# Production Caveats & Troubleshooting

Review every item in this guide before deploying to staging or production.

---

## 1. Critical Looker Core Platform Caveats

### 1.1 Viewer License Tier Trap (Cost Impact)
- **Trap**: Granting `explore` or `develop` (or any Standard/Developer permission) to the coarse onboarding SAML group immediately promotes **every** SAML user—including unentitled users waiting for approval—out of the **Viewer** license tier.
- **Mitigation**: Create a dedicated Permission Set containing **`access_data` ONLY**, bound to a Model Set containing only the empty `provisioning_landing` model (`lookml/provisioning_landing.model.lkml`). Verify during Phase 0 that `sdk.me('id,group_ids')` works under this minimal role.

### 1.2 SAML Mirroring + Advanced Role Management Trap (Silent Wipe)
- **Trap**: If **Mirror SAML Groups** is enabled in Looker Admin -> Authentication -> SAML, enabling either of these toggles causes Looker to **strip API-assigned groups on the user's next SAML login**:
  - *Prevent Direct Membership in non-SAML Groups*
  - *Prevent Role Inheritance from non-SAML Groups*
- **Mitigation**: Leave **both toggles OFF** whenever mixing coarse SAML groups with Looker API group assignment. Always verify by logging out and logging back in twice during Phase 0.

### 1.3 Live Session Permission Refresh (`reload` vs `spa` vs `logout`)
- **Caveat**: Depending on Looker Core version and session cache behavior, adding a user to a group via API 4.0 may take effect on the next full page load (`reload`), immediately on SPA navigation (`spa`), or only after session re-establishment (`logout`).
- **Mitigation**: Keep `autoRedirect: false` in `extension/src/config.js` during Phase 0 / UAT. The ready screen presents buttons for **Full Reload**, **In-App Navigation**, **New Tab**, and **Re-login (`/logout`)**. Test which option displays the target dashboard immediately, set `redirectMode` accordingly, and only then flip `autoRedirect: true`.
  - If `logout` is required, enable **Bypass Login Page** in Looker SAML settings so `/logout` -> IdP SSO re-enters seamlessly without prompting for a password.

### 1.4 Monthly Looker Core Admin API Limits
- Standard (1K/mo), Enterprise (10K/mo), Embed (100K/mo).
- Keep `NO_LANDING_PAGE=true` (use group-ranked `landing_page`), nest sub-groups into persona groups, move high-cardinality RLS to BigQuery (`lookml/warehouse_rls.example.lkml`), and use the Enterprise Firestore ledger to drop repeat logins with 0 Looker API calls.

### 1.5 Testing Constraint: No `sudo` or `login_user` on Looker Core
- Looker (Google Cloud core) restricts `sudo` and `POST /api/4.0/login/{user_id}` for standard accounts.
- **Mitigation**: Allocate 3–5 fresh throwaway SAML accounts in your IdP for Phase 0 and UAT testing, because true Day-1 JIT user creation only occurs on an account's very first sign-in.

---

## 2. Enterprise Control Plane Caveats

### 2.1 Verify Exact Cloud Logging `logName` in Phase 0
- Looker Core environments may emit user login logs under `looker.googleapis.com%2Fuser_login` or `looker.googleapis.com%2FUserLogin`.
- **Mitigation**: Perform one test SAML login, inspect the entry in **Cloud Logging -> Logs Explorer**, confirm the exact `logName` and `jsonPayload.actor.id` path, and set `looker_login_log_name` in `infra/enterprise/terraform.tfvars`.

### 2.2 Service Account Feedback Loop Prevention
- Looker's login event fires for both interactive UI logins **and** API 4.0 logins (including the Worker's own `/api/4.0/login` calls).
- **Mitigation (defense in depth)**:
  1. Exclude the Provisioning API Service Account's numeric Looker user ID at the **Log Router Sink** (`provisioning_sa_looker_user_id`).
  2. Pass the same ID in `EXCLUDED_ACTOR_IDS` to Cloud Run Gate (`log_parser.py`).
  3. Worker refuses to modify any Looker user with `is_service_account == true` (`provisioner.py`).

### 2.3 Cloud Logging Latency Expectations
- Cloud Logging -> Log Router -> Pub/Sub -> Cloud Run Gate -> Cloud Tasks -> Worker typically completes in **2–5 seconds**, but Cloud Logging does not offer a hard `<1s` delivery SLA.
- **Mitigation**: `extension/src/config.js` defaults `pollIntervalsMs` to `[2500, 4000, 7000, 12000, 20000]` (with +/- 15% jitter) so typical logins resolve in 1–2 checks while slow log propagation degrades gracefully up to ~45 seconds before showing a manual **Check Again** button.

### 2.4 Deep Links (`https://looker.example.com/dashboards/42`)
- When a first-time user clicks a direct link to a dashboard, Looker routes them directly to `/dashboards/42` (bypassing `landing_page`), which returns 404 / "no access" until their groups arrive.
- **Mitigations**:
  - In Enterprise mode, the **Cloud Logging pipeline STILL triggers on deep-link logins**, so refreshing the link 3–5 seconds later succeeds automatically.
  - For corporate intranet / portal links, link to `/extensions/provisioning_landing::jit_onboarding?next=/dashboards/42` so first-time users see the splash screen and land straight on dashboard 42 once provisioned.

### 2.5 Safe Delta Revocation Scope
- Revoking groups blindly would risk stripping `All Users` or IdP-mirrored SAML groups.
- **Mitigation**: `Provisioner` only ever removes groups whose name starts with `MANAGED_GROUP_PREFIX` (default `BI_`) or is listed in `MANAGED_GROUPS`, and unconditionally skips `All Users` (`id == "1"`) and any group with `externally_managed == true`.

### 2.6 Zero-PII Firestore Ledger & Drift Self-Healing
- Firestore stores only `looker_user_id`, status, timestamps, and an opaque SHA-256 `entitlement_hash`—never emails or group names.
- If an admin manually removes a user from a Looker group while the ledger entry is fresh (< 24 h), either run `POST /tasks/reconcile` with `{"emails": ["user@example.com"]}` or delete that user's ledger document so the next login re-provisions.

---

## 3. SMB Profile & Extension Caveats

### 3.1 Private IP Looker Core Requires Enterprise Profile
- If Looker Core is Private IP-only, Looker's `serverProxy` cannot reach external HTTPS endpoints without configuring **Private Service Connect (PSC) Southbound** (Internal TCP Proxy LB + NEG + publicly trusted TLS certificate).
- Use the **Enterprise profile (`manifest.enterprise.lkml`, `external_api_urls: []`)** on Private IP instances to avoid PSC Southbound completely.

### 3.2 `fetchProxy` vs `serverProxy` in SMB Mode
- `transport: 'fetch'` sends requests from the **user's browser** (requires CORS `ALLOWED_ORIGINS` and cannot hold a hidden shared secret). Use for local PoC only.
- `transport: 'server'` routes requests through Looker's backend with `createSecretKeyTag`, keeping `PROVISIONING_SHARED_SECRET` hidden in a scoped user attribute (`scoped_user_attributes` in `manifest.smb.lkml`). Required for production SMB deployments.

### 3.3 Spartan ("Extensions Only") Mode Warning
- Do **not** set the coarse onboarding group to Spartan / `Extensions Only` mode unless you verify that Looker exits Spartan mode immediately upon adding the higher-priority business group. Otherwise users remain trapped inside the Extension even after provisioning.

### 3.4 Multi-Replica Caching & Concurrency
- Both SMB and Enterprise containers cache Looker group-name->ID maps and the 50-minute Looker API token in process memory. Keep `max_instances` bounded (`<= 2` SMB, `<= 4` Enterprise Worker) and rely on Cloud Tasks (`jit-onboarding-high` / `reconcile-low`) in Enterprise mode to bound global Looker API concurrency across replicas.

---

## 4. Quick Troubleshooting Table

| Symptom | Likely Cause | Fix |
| --- | --- | --- |
| Extension shows blank white iframe or console error `process is not defined` | Bundled without Webpack 5 polyfills | Ensure `extension/webpack.config.cjs` + `package.json` (`buffer`, `process`) are used and rebuild (`npm run build`). |
| Extension redirects in a loop back to itself | User's `landing_page` or `hubPath` still points to the extension | Verify business group has higher priority than coarse SAML group, or set `hubPath: '/browse'`. `flow.js` aborts self-redirect loops cleanly. |
| User gets provisioned on Login #1, then loses groups on Login #2 | SAML Mirroring *Prevent Direct Membership* toggle is ON | Turn OFF both *Prevent Direct Membership* and *Prevent Role Inheritance* in SAML settings. |
| Enterprise Worker never triggers on login | Wrong `looker_login_log_name` or sink lacks `roles/pubsub.publisher` | Inspect exact `logName` in Logs Explorer; verify `terraform apply` completed IAM bindings in `infra/enterprise/iam.tf`. |
| Pub/Sub messages pile up in Dead-Letter Topic | Gate returns 400 (unexpected log schema) or 503 (Firestore/Tasks IAM missing) | Pull 1 message from `<prefix>-user-logins-dlt-replay` and check Gate Cloud Run logs. |
| Worker returns 503 on `/tasks/provision` | Direct VPC Egress cannot reach Looker PSC ALB or IGA API, or bad API3 creds | Check Worker logs, subnet firewall/PSC routing, and Secret Manager secret versions. |
| User gets stuck on "Setting up your workspace" for 45 s | User has no entitlements AND `pendingMarkerGroupId` is unset | Create `BI_Access_Pending_Approval`, pass its numeric ID to `config.js` (`pendingMarkerGroupId`), and set `PENDING_MARKER_GROUP=BI_Access_Pending_Approval` on Worker. |
| SMB `/provision` returns `401 Unauthorized` | Mismatched `PROVISIONING_SHARED_SECRET` or missing `scoped_user_attributes` | Confirm hidden Looker user attribute `<project>_<secretKeyName>` matches Secret Manager `shared-secret`. |
| SMB `/provision` returns `403 Forbidden` | Browser `Origin` missing from `ALLOWED_ORIGINS` | Set `ALLOWED_ORIGINS` to exact Looker URL (no trailing slash) or switch to `transport: 'server'`. |

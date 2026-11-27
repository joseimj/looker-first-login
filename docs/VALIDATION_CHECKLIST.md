# Validation Checklist (Phase 0, SMB & Enterprise)

Complete **Phase 0** (~30 minutes, zero custom code deployed) on a dev/UAT Looker (Google Cloud core) instance before starting full infrastructure rollout.

> **Test account note**: Looker Core disables `sudo` and `login_user` for regular accounts. Provision **4 fresh throwaway SAML accounts** in your IdP (`test-u1`, `test-u2`, `test-u3`, `test-u4`) that have **never** logged into Looker Core before.

---

## Phase 0: Zero-Code Looker Core Platform Verification

| # | Check | How to Test | Pass Criteria |
| --- | --- | --- | --- |
| **P0-1** | **Viewer-tier coarse SAML role** | Create model set `provisioning_landing` + permission set with **`access_data` ONLY**. Map coarse SAML group `SAML_BI_Users` to this role. Log in with `test-u1`. | Looker Admin -> Users shows `test-u1` in **Viewer** tier (NOT Standard or Developer). |
| **P0-2** | **First-login `landing_page` honor** | Set `SAML_BI_Users` `landing_page` user attribute to `/extensions/provisioning_landing::jit_onboarding` (or a test folder URL). Log in with brand-new `test-u2`. | `test-u2` lands directly on the configured `landing_page` on Login #1 (not a blank `/browse`). |
| **P0-3** | **Immediate session permission refresh** | Keep `test-u2` signed in. Run `backend/verify_immediacy.py` (or API `add_group_user`) to add a business group (`BI_Dashboard_Hub`). Try (a) SPA navigation, (b) browser refresh (`reload`), (c) `/logout` + SSO. | Record which transition immediately renders the target dashboard -> set `redirectMode` in `extension/src/config.js`. |
| **P0-4** | **SAML Mirroring persistence** | Have `test-u2` log out and log back in **twice**. | `test-u2` retains `BI_Dashboard_Hub` across repeated SAML logins (proves *Prevent Direct Membership* is OFF). |
| **P0-5** | **`sdk.me('id,group_ids')` under Viewer role** | Load the extension as `test-u1` (`autoRedirect: false`). While extension is open, add `BI_Dashboard_Hub` via API. | `sdk.me('id,group_ids')` succeeds under `access_data`-only role and reflects the newly added group ID without re-login. |
| **P0-6** | *(Enterprise)* **Cloud Logging `user_login` schema & latency** | Log in as `test-u3`. Query Cloud Logging Logs Explorer for `resource.type="looker.googleapis.com/Instance"`. | Record exact `logName` (`user_login` vs `UserLogin`), verify `jsonPayload.actor.id` matches Looker user ID, note log arrival delay (typically 1–4 s), and record the numeric `actor.id` of your Provisioning API Service Account. |

---

## SMB Profile E2E Sign-Off Matrix

| # | Scenario | Expected Result |
| --- | --- | --- |
| **S-1** | Unauthenticated `POST /provision` (wrong/missing `X-Provisioning-Secret`) | `401 Unauthorized`; zero Looker API calls made. |
| **S-2** | New entitled SAML user (`test-u3`) logs in with `mode: 'direct'`, `transport: 'server'` | Splash screen displays -> `/provision` returns `200 {status: "provisioned"}` in < 3 s -> user reaches Dashboard Hub with data visible. |
| **S-3** | Same user reloads the extension within 5 min | Returns `cached: true` (0 Looker API calls) and redirects cleanly. |
| **S-4** | New unentitled SAML user (`test-u4`, not in entitlements JSON) logs in | Backend returns `status: "no_entitlements"` (fail-closed, 0 business groups added); Extension displays friendly "No dashboards assigned yet" screen. |
| **S-5** | Misconfigured/unavailable backend | Extension retries per `maxAttempts` then displays "Try Again" button (no infinite loop). |
| **S-6** | Admin API call count | Inspect backend logs: `<= 3` Admin API calls per new user when `NO_LANDING_PAGE=true` and persona groups are used. |

---

## Enterprise Profile E2E Sign-Off Matrix

| # | Scenario | Expected Result |
| --- | --- | --- |
| **E-1** | Zero-egress manifest verification | Confirm `manifest.lkml` contains `external_api_urls: []` and Browser DevTools Network tab shows **zero** calls outside Looker's origin. |
| **E-2** | Day-1 entitled SAML user (`test-u3`) logs in | `user_login` -> Log Sink -> Pub/Sub -> Gate (Firestore lease acquired) -> `jit-onboarding-high` -> Worker (Direct VPC -> PSC ALB -> `add_group_user`) -> Extension detects new group via `sdk.me()` and redirects to Dashboard Hub in < 10 s. |
| **E-3** | Deep-link via portal (`?next=/dashboards/42`) | User sees splash screen and redirects straight to `/dashboards/42` (not generic `/browse`). |
| **E-4** | Repeat login by already-provisioned `test-u3` | Gate logs `action: "skip_fresh"` in ~15 ms -> **0** Cloud Tasks created, **0** Looker API calls. |
| **E-5** | Provisioning SA API login | Log Router Sink excludes SA `actor.id` -> **0** Pub/Sub messages emitted (zero feedback loop). |
| **E-6** | Zero-PII Firestore audit | Inspect Firestore collection `looker_jit_ledger`: documents contain only numeric ID, status, timestamps, and SHA-256 `entitlement_hash`—**no email addresses or group names**. |
| **E-7** | Day-1 unentitled SAML user (`test-u4`) logs in | Worker assigns `BI_Access_Pending_Approval` -> Extension stops polling immediately on next tick and renders "No dashboards assigned" + `accessRequestUrl` link. |
| **E-8** | Delta revocation (`POST /tasks/reconcile`) | Remove `BI_EMEA_Finance` from `test-u3` in IGA -> Cloud Scheduler triggers `/tasks/reconcile` -> enqueued to `reconcile-low` -> Worker removes `BI_EMEA_Finance` while **preserving** `All Users` and `SAML_BI_Users`. |
| **E-9** | Dead-Letter Topic resilience | Temporarily inject a malformed Pub/Sub message -> after 5 attempts, message appears in `<prefix>-user-logins-dlt-replay` subscription for operator inspection. |
| **E-10** | Plane 3 Warehouse-Native RLS | Query a BigQuery Explore using `lookml/warehouse_rls.example.lkml` as `test-u3` -> rows filtered by `user_rls_entitlements` with 0 Looker User Attribute API writes; flip `is_active = FALSE` in BigQuery -> next query returns 0 rows immediately. |

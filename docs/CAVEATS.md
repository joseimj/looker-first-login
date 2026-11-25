# Errors, Caveats, and Production Readiness Checklist

Review these caveats before running the PoC and before scaling this pattern to
production.

## 1. Two behaviors the PoC must verify first

Neither behavior is explicitly guaranteed in the Looker documentation. Run
**Stage 0** in the [README](../README.md) on a test instance before investing in
production plumbing:

1.  **`landing_page` on the very first SAML login.** Confirm that a brand-new
    SAML user lands on the coarse group's `landing_page` immediately on first
    authentication (before any second login).
2.  **Live session refresh after `add_group_user`.** Confirm whether the user's
    existing browser session sees newly granted folders/roles after:
    *   in-app navigation (`extensionSDK.updateLocation(target)`),
    *   a full page load (`extensionSDK.updateLocation(target, undefined, '_self')`),
    *   a new tab (`extensionSDK.openBrowserWindow(target, '_blank')`), or
    *   only after a fresh SAML login.
    If only a fresh login works, redirect to `/logout` (with **Bypass Login
    Page** enabled on the SAML config) instead of directly to the dashboard,
    and test that the IdP session transparently re-authenticates the user.

## 2. Licensing pitfall: coarse SAML role permissions

Looker classifies users into license tiers (Viewer, Standard/Creator,
Developer) based on the **highest permission** granted across all of their
roles.

*   Grant the coarse SAML onboarding role **only `access_data`** on a model set
    containing **only** the empty `provisioning_landing` model.
*   Do **not** grant `explore` or `develop` to the coarse SAML group: doing so
    classifies every onboarded user as a Standard (Creator) or Developer
    license, even if they are only meant to be Viewers.
*   Verify in Stage 1 that `access_data` alone is sufficient to load the
    extension and call `sdk.me('id')` on your Looker version.

## 3. Looker (Google Cloud core) monthly Admin API call allotment

Looker (Google Cloud core) editions document monthly Admin API call allotments:

| Edition | Monthly Query API calls | Monthly Admin API calls |
| :--- | ---: | ---: |
| Standard | 1,000 | 1,000 |
| Enterprise | 100,000 | 10,000 |
| Embed | 500,000 | 100,000 |

User, group, and user-attribute API calls count as **Admin API calls** (logins
do not). Your scheduled reconciliation job shares the same monthly pool.

**Per-user cost in this PoC:**
*   1 read (`GET /users/{id}`) + 1 call per group (`add_group_user`) + 1 call
    per RLS user attribute (`set_user_attribute_user_value`) + 1 call for
    `landing_page` (unless `--no-landing-page` is used).
*   Example: 3 groups + 1 RLS attribute + `landing_page` = **6 Admin API calls
    per new user** (60,000 calls for 10,000 users).

**Ways to reduce calls before rollout:**
1.  **Use group-ranked `landing_page` instead of per-user `landing_page`
    (`--no-landing-page`).** Assign the Dashboard Hub URL to a provisioned
    group ranked *above* the coarse SAML group in **Admin > User Attributes >
    landing_page > Group Values**. This saves 10,000 API calls *and* keeps the
    hub URL centrally editable (a per-user value overrides group values
    permanently, so changing the hub URL later would require patching 10,000
    individual users).
2.  **Use wrapper/profile groups.** Add each user to one composite Looker group
    that contains their fine-grained groups (verify that folder access and
    roles inherit as expected in your Looker setup).
3.  **Deliver RLS attributes via SAML attribute pairings.** Looker applies SAML
    user-attribute pairings automatically on every login (including the first)
    with zero API calls.
4.  **Confirm your edition and overage behavior** with your Google Cloud
    account team before rolling out to 10,000 users.

## 4. SAML "Mirror SAML Groups" + Advanced Role Management trap

If **Mirror SAML Groups** is enabled in Looker **Admin > Authentication > SAML**:

*   **Prevent Direct Membership in non-SAML Groups** must be **OFF**.
*   **Prevent Role Inheritance from non-SAML Groups** must be **OFF**.

If either toggle is ON (the default on instances that previously configured
mirrored groups), Looker **strips all API-assigned group memberships or roles
on the user's next SAML login**. Stage 0 step 7 explicitly verifies this.

## 5. Deep links, portals, and deprovisioning

*   **Deep links skip `landing_page`.** When a first-time user opens a shared
    dashboard URL (for example `/dashboards/42`), Looker forwards them to that
    URL after SAML authentication rather than to `landing_page`. They will see
    a "not found / no permission" page until provisioned.
*   **Keep your scheduled sync job.** The landing extension only accelerates
    first-time logins arriving at the root URL. Keep the scheduled job to cover
    deep-link arrivals, entitlement changes, and access removal/offboarding.

## 6. Row-level security (RLS) and partial-failure safety

*   The backend always writes in fail-closed order: **RLS user attributes
    first**, **group memberships second**, **`landing_page` last**.
*   A user is never added to a data-bearing group before their RLS attributes
    are in place.
*   If a run fails partway through (for example after adding groups, before
    updating `landing_page`), the user lands on the extension again and the
    backend inspects current attribute values to repair only what is missing.
*   If any Looker group or user attribute named in entitlements does not exist,
    the backend fails with HTTP 500 **before** performing any write.

## 7. Security and identity model

*   **Zero trust in client identity beyond `user_id`.** The extension sends
    only `{user_id}` from `sdk.me('id')`. The backend calls `GET /users/{id}`
    with its own Admin API credentials to read the authoritative email (falling
    back to `credentials_saml.email`), and derives every group, attribute, and
    redirect solely from the entitlements store.
*   **Production authentication for `/provision`.**
    *   In **Stage 2 (`serverProxy`)**, enable `PROVISIONING_SHARED_SECRET` and
        store the secret in a hidden Looker scoped user attribute
        (`provisioning_landing_splash_backend_secret`) with **User Access:
        None** (or View), **Hide Values: Yes**, **Domain Allowlist** restricted
        to the backend HTTPS URL, and `scoped_user_attributes:
        ["backend_secret"]` in `manifest.lkml`. The secret never reaches the
        browser.
    *   In **Stage 1 (`fetchProxy`)**, browser calls cannot hide a static
        secret. Restrict `--allowed-origin` to the exact Looker origin, keep
        the backend on a private network, or switch to `serverProxy` for
        production.
*   **Enable `--require-saml` in production** so backup Google OAuth accounts
    or non-SAML accounts are never modified by the provisioning endpoint.
    API-only service accounts and disabled users are always skipped.
*   **Never enable `--debug-response` in production** (it exposes assigned
    group/attribute names to the browser).

## 8. Browser, networking, and Looker Extension quirks

| Issue | Cause | Fix |
| :--- | :--- | :--- |
| Extension shows "page not found" | Project not deployed to Production, model missing a valid connection, or user role lacks `access_data` on the model set | Commit and deploy to Production; check model configuration and coarse group role |
| `fetchProxy` blocked immediately | `external_api_urls` in `manifest.lkml` does not match `backendUrl` in `extension/src/config.js` | Align both URLs, rebuild/redeploy LookML, hard-refresh Looker |
| Browser blocks `https://` Looker -> `http://localhost:8787` | Chrome Private Network Access prompt or Safari mixed-content policy | Accept Chrome's local-network prompt (`Access-Control-Allow-Private-Network: true` is sent by `server.py`), test in Chrome/Edge, or move to Stage 2 HTTPS |
| `serverProxy` cannot reach backend | Looker Core instance uses Private IP / PSC without egress to the backend, or backend is on `localhost` | Expose backend over HTTPS reachable from Looker (e.g., Cloud Run, or PSC producer / hybrid egress for Private IP Looker Core), or use `fetchProxy` from corporate browsers |
| `serverProxy` returns HTTP 401 | Looker sent literal `{{provisioning_landing_splash_backend_secret}}` instead of substituting the secret | Verify: (1) `scoped_user_attributes: ["backend_secret"]` is uncommented in `manifest.lkml`, (2) user attribute name uses single underscores (`::` -> `_`), (3) **Hide Values = Yes**, (4) **Domain Allowlist** matches the backend URL, (5) Default Value matches `PROVISIONING_SHARED_SECRET` |
| Webpack 5 build error (`process` / `buffer` not found) | `@looker/extension-sdk` references Node globals | Already wired in `extension/webpack.config.cjs` + `package.json`; run `npm install` in `extension/` |
| Spartan mode locks users out of dashboards | Adding users to Looker's built-in `Extensions Only` group blocks all standard Looker routes | Do **not** add users to `Extensions Only`. If you want to hide Looker's nav bar on the splash screen, test `/spartan/provisioning_landing::splash` without that group |
| Infinite redirect loop | `landing_page` or `fallbackPath` points back to `/extensions/provisioning_landing::splash` | `flow.js` blocks self-redirects and fails safely; ensure provisioned destinations are real dashboards/boards |
| Cannot test with Looker `sudo` or `login_user` | Looker Core disables `sudo` and `POST /api/4.0/login/{user_id}` for regular users | Use fresh throwaway SAML test accounts (never an existing admin) for every test |

## 9. Scaling to a 10,000-user rollout (production hardening)

1.  **Rollout waves (avoid a Day-1 thundering herd).** If 1,000 users sign in at
    9:00 AM, concurrent calls can trigger Looker `429 Too Many Requests` rate
    limiting. Roll out in cohorts (for example 200–500 users per wave).
    `looker_api.py` caps concurrent Looker calls (`--max-concurrency`, default
    10) and honors `Retry-After` headers.
2.  **Multi-replica backends (Cloud Run / Kubernetes).**
    *   Looker API `POST /login` with one `client_id`/`client_secret` shares a
        single active token track; avoid calling `POST /logout` from individual
        replicas, and consider caching/sharing the token or bounding instance
        count.
    *   `server.py`'s in-memory cache and per-user locks are per-process.
        `provisioner.py` handles cross-replica `409` group-add races
        idempotently, but for high-concurrency production you should share the
        short-lived cache (for example Redis/Memorystore) and replace
        `entitlements.json` with your entitlements database.
3.  **Observability.** Monitor `status`, `admin_api_calls`, `elapsed_ms`, and
    HTTP `429`/`5xx` rates from the structured backend logs.

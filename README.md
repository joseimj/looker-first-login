# First-Login Provisioning PoC for Looker (Google Cloud core)

Looker (Google Cloud core) creates user accounts just-in-time on first login.
When fine-grained access is managed via internal Looker groups and the Looker
API (rather than mirroring hundreds of AD groups in SAML), first-time users can
land on an empty home screen until a scheduled sync job assigns their groups
and user attributes.

This repository provides a minimal, end-to-end **Proof of Concept (PoC)** that
replaces that blank screen with a lightweight Looker Extension set as the
`landing_page` of the coarse SAML onboarding group:

```text
First SAML login
  └─► landing_page = /extensions/provisioning_landing::splash
        │ 1. sdk.me('id')                       (identify signed-in user)
        │ 2. POST /provision {user_id} ───────► Provisioning backend
        │                                         a. GET /users/{id} (authoritative email)
        │                                         b. Look up entitlements
        │                                         c. Set RLS user attributes (fail-closed)
        │                                         d. Add Looker groups (add_group_user)
        │                                         e. Update landing_page (optional)
        │ 3. {status, redirect}        ◄───────
        └─► 4. Redirect to user's dashboards
```

> **Before deploying:** Read [docs/CAVEATS.md](docs/CAVEATS.md) for licensing
> tier rules (`access_data` only), monthly Admin API call budgets, SAML
> Advanced Role Management settings, and networking/security notes.

## Repository layout

```text
looker_first_login_poc/
├── README.md                                 # This runbook
├── docs/CAVEATS.md                           # Known errors, caveats & production checklist
├── lookml/
│   ├── manifest.lkml                         # Looker Extension manifest & entitlements
│   └── provisioning_landing.model.lkml       # Empty LookML model for extension access control
├── extension/
│   ├── package.json                          # @looker/extension-sdk + Webpack 5 build/dev scripts
│   ├── webpack.config.cjs                    # Bundles src/ -> dist/bundle.js with Node polyfills
│   ├── src/
│   │   ├── config.js                         # Backend URL, transport, redirect mode, retry settings
│   │   ├── flow.js                           # Provisioning state machine, retries, loop guard
│   │   ├── ui.js                             # Splash screen (plain DOM, localized message table)
│   │   └── index.js                          # Extension host bootstrap
│   └── test/                                 # Node unit tests (no external test runner needed)
└── backend/
    ├── Dockerfile                            # Zero-dependency container for Stage 2 (Cloud Run)
    ├── server.py                             # HTTP server exposing POST /provision & GET /healthz
    ├── provisioner.py                        # Fail-closed, idempotent provisioning engine
    ├── looker_api.py                         # Stdlib Looker API 4.0 client (token cache, retries)
    ├── verify_immediacy.py                   # CLI tool timing how fast add_group_user takes effect
    ├── entitlements.example.json             # Sample entitlements map (copy to entitlements.json)
    ├── fake_looker.py                        # In-memory fake Looker API for unit tests & offline demo
    └── test_*.py                             # Python unittest suite
```

**Prerequisites:**
*   Backend: Python 3.9+ (standard library only, zero pip dependencies).
*   Extension: Node.js 20+ and npm.
*   Looker: A test Looker (Google Cloud core) instance, Admin access, and fresh
    test accounts that have never signed in (Looker Core does not support
    `sudo` or `login_user` for regular users).

## Current verification status

*   **Tested locally:** Backend (`python3 -m unittest`) and extension logic/UI
    (`npm test`) pass unit tests against an in-memory fake Looker API and DOM
    stand-ins.
*   **Must be verified on your Looker instance:** Building `dist/bundle.js` via
    `npm run build`, confirming `landing_page` fires on first SAML login, and
    confirming whether `add_group_user` refreshes access in the current browser
    session without re-login.

## Questions this PoC answers

1.  Does Looker route a brand-new SAML user to their coarse group's
    `landing_page` on their **first** login?
2.  Can a user whose coarse role has **only `access_data`** on the extension's
    model load the extension and call `sdk.me('id')` (keeping them in the
    Viewer license tier)?
3.  After `add_group_user`, does the user's **current** browser session gain
    access via in-app navigation, via full page reload, via a new tab, or only
    after a fresh login?
4.  Do API-assigned group memberships survive the user's **next** SAML login?
5.  How many Admin API calls does onboarding one user consume?

---

## Stage 0: Validate core Looker behavior with zero code (~20 min)

Run this first on a test instance to answer Questions 1, 3, and 4 before
building any code.

1.  Identify the group every new user belongs to at first login:
    *   SAML with **Mirror SAML Groups** ON: your coarse mirrored SAML group.
    *   SAML with **Mirror SAML Groups** OFF: a group in **New User Groups**.
    *   Google OAuth test instance (no group mirroring): set the instance
        default homepage (**Admin > General > Homepage**) instead of a group
        value, and skip step 2.
2.  In **Admin > Users > User Attributes > `landing_page` > Group Values**,
    assign that group a recognizable page (for example `/browse/boards/<id>` or
    an existing extension URL).
3.  Sign in as a brand-new test user at the root URL (`https://<instance>/`) in
    a private browser window. Record where they land (**Question 1**).
4.  Sign in as a second brand-new user through a deep link (for example
    `https://<instance>/dashboards/<id>`). Confirm they go to the deep link
    rather than `landing_page` (expected Looker behavior).
5.  Keep the first user signed in. As an admin, add them to a group that grants
    a role and access to a restricted folder (`add_group_user` via API Explorer
    or **Admin > Groups**).
6.  In the user's browser, navigate to that folder in-app (without reloading),
    then reload the page. Record which mode shows the content (**Question 3**).
7.  Sign out and sign back in as that user. Verify on **Admin > Users** that
    the API-added group is still present (**Question 4**). If it disappeared
    and **Mirror SAML Groups** is ON, check **Admin > Authentication > SAML >
    Advanced Role Management** and turn **OFF** *Prevent Direct Membership in
    non-SAML Groups* and *Prevent Role Inheritance from non-SAML Groups*.

---

## Stage 1: End-to-end PoC (Extension + local backend)

### 1. Configure Looker

1.  Create a blank LookML project named `provisioning_landing` (a bare Git
    repository is sufficient for a PoC).
2.  Add `lookml/manifest.lkml` and `lookml/provisioning_landing.model.lkml`.
    Replace `REPLACE_WITH_A_CONNECTION_NAME` in the model file with any valid
    database connection on the instance, and allow that connection for the
    model in Project Configuration.
3.  Create a minimal onboarding role:
    *   **Permission Set:** `access_data` only (do **not** include `explore` or
        `develop`, which upgrades Viewer licenses; see
        [docs/CAVEATS.md](docs/CAVEATS.md)).
    *   **Model Set:** `provisioning_landing` only.
    Assign this role to your Stage 0 onboarding group (in **Manage Groups** on
    the SAML page if mirroring is ON, or **New User Roles** if mirroring is
    OFF).
4.  Set `landing_page` for that onboarding group to:
    ```text
    /extensions/provisioning_landing::splash
    ```
    *(Optional test: `/spartan/provisioning_landing::splash` hides Looker's left
    nav during the splash screen. Do **not** add users to the `Extensions Only`
    group.)*
5.  Create the target Looker groups, folder permissions, and RLS user
    attributes referenced in your entitlements file (the backend does not
    create groups).
6.  Create an API-only Service Account with the Admin role (**Admin > Users >
    Add Service Accounts**) and copy its `client_id` and `client_secret`.

### 2. Build and deploy the extension

```bash
cd extension
npm install
npm test
npm run build
```

Upload `extension/dist/bundle.js` to the root of the `provisioning_landing`
LookML project, commit, and **deploy to Production** (only users in Development
Mode see undeployed changes).

*Tip for fast iteration:* You can run `npm run develop` locally and temporarily
swap `file: "bundle.js"` for `url: "http://localhost:8080/bundle.js"` in
`manifest.lkml` while developing in Dev Mode.

`extension/src/config.js` defaults to `autoRedirect: false` so the splash
screen pauses after provisioning and displays three buttons (**Continue (page
reload)**, **Continue (in-app navigation)**, **Open in new tab**) to verify
**Question 3**. Once you know which redirect mode works on your instance, set
`redirectMode` accordingly and switch `autoRedirect: true`, then rebuild and
redeploy `bundle.js`.

### 3. Start the backend

```bash
cd backend
cp entitlements.example.json entitlements.json
# Edit entitlements.json with your test user email, Looker group names,
# optional RLS user_attributes, and target landing_page.

export LOOKER_CLIENT_ID="<service-account-client-id>"
export LOOKER_CLIENT_SECRET="<service-account-client-secret>"

python3 server.py \
  --looker-base-url "https://<your-looker-instance>" \
  --entitlements entitlements.json \
  --allowed-origin "https://<your-looker-instance>" \
  --debug-response
```

*   Add `--dry-run` for a first pass that logs the plan without writing to
    Looker.
*   Add `--no-landing-page` if you use a higher-ranked group `landing_page`
    instead of per-user `landing_page` overrides (saves 1 Admin API call per
    user).
*   Add `--require-saml` when testing SAML accounts to ensure non-SAML accounts
    are skipped.
*   Because Stage 1 uses `fetchProxy`, the browser calls
    `http://localhost:8787/provision` directly. Run `server.py` on the same
    machine as your browser (Chrome/Edge recommended; accept the Local Network
    Access prompt if shown). If browser/network policies block `localhost`,
    proceed to Stage 2.

### 4. PoC verification matrix

| # | Test case | Expected result | Observed |
| :- | :--- | :--- | :--- |
| 1 | Fresh SAML user signs in at root URL | Splash screen loads without blank screen | |
| 2 | Coarse role has only `access_data` on `provisioning_landing` | Extension loads and `sdk.me('id')` succeeds | |
| 3 | Backend completes provisioning | Status reaches `Your access is ready.`; backend logs `status: provisioned` | |
| 4 | Click **Continue (in-app navigation)** | Target dashboard/folder loads with data | |
| 5 | Second fresh user: click **Continue (page reload)** | Target dashboard/folder loads with data | |
| 6 | Check data filtering | RLS user attributes filter rows immediately | |
| 7 | Sign out and sign in again | Lands directly on dashboards; API groups persist | |
| 8 | Fresh user opens deep link directly | Skips splash screen (record exact UX) | |
| 9 | User missing from `entitlements.json` | Friendly "no dashboards assigned" message | |
| 10 | Backend stopped / unreachable | Retries automatically, then shows **Try again** | |
| 11 | Record `admin_api_calls` from backend log | Compare with monthly edition allotment | |

---

## Stage 2: Server-to-server (`serverProxy` + Cloud Run + shared secret)

Use Stage 2 for shared testing or when moving toward production: Looker calls
the backend server-to-server over HTTPS, eliminating browser CORS / local
network issues and keeping a shared secret out of the browser.

1.  Deploy `backend/` as an HTTPS service (for example on Cloud Run using
    `backend/Dockerfile`), setting `LOOKER_BASE_URL`, `LOOKER_CLIENT_ID`,
    `LOOKER_CLIENT_SECRET`, `PROVISIONING_SHARED_SECRET`, `ENTITLEMENTS_PATH`,
    and `REQUIRE_SAML=true`. *(Note: Private IP / PSC Looker Core instances
    require an egress route to the backend.)*
2.  In Looker **Admin > Users > User Attributes**, create a user attribute:
    *   **Name:** `provisioning_landing_splash_backend_secret`
    *   **User Access:** `None` (or `View`)
    *   **Hide Values:** `Yes`
    *   **Domain Allowlist:** `https://<your-backend-host>/*`
    *   **Default Value:** same string as `PROVISIONING_SHARED_SECRET`
3.  In `lookml/manifest.lkml`:
    *   Update `external_api_urls` to `["https://<your-backend-host>"]`
    *   Uncomment `scoped_user_attributes: ["backend_secret"]`
4.  In `extension/src/config.js`:
    *   Set `backendUrl` to `'https://<your-backend-host>/provision'`
    *   Set `transport: 'server'` and `secretKeyName: 'backend_secret'`
5.  Rebuild (`npm run build`), upload `dist/bundle.js`, and deploy LookML to
    Production.

---

## Optional: Scripted API immediacy check (`verify_immediacy.py`)

Complements Stage 0 step 6 by measuring how fast `add_group_user` takes effect
at the Looker API layer. It creates a temporary API key on a throwaway test
user, verifies no access, calls `add_group_user`, polls the probe with the
existing API session (and a fresh login if needed), then cleans up both the
membership and the temporary key.

```bash
cd backend
export LOOKER_CLIENT_ID="<admin-client-id>"
export LOOKER_CLIENT_SECRET="<admin-client-secret>"

python3 verify_immediacy.py \
  --looker-base-url "https://<your-looker-instance>" \
  --subject-user-id "<test-user-id>" \
  --group-id "<target-group-id>" \
  --probe-path "/folders/<restricted-folder-id>"
```

Verdicts: `IMMEDIATE` (first probe succeeded), `DELAYED` (succeeded after a
wait), `RELOGIN_REQUIRED` (only a fresh login had access), or `NOT_EFFECTIVE`
(check group folder permissions and SAML Advanced Role Management settings).

---

## Offline demo & automated tests (no Looker instance needed)

Run the automated test suites:

```bash
cd backend && python3 -m unittest discover -p 'test_*.py'
cd extension && npm test
```

Run an offline end-to-end demo against the bundled in-memory fake Looker:

```bash
cd backend
python3 fake_looker.py --port 9999 &
export LOOKER_CLIENT_ID=demo-admin-id LOOKER_CLIENT_SECRET=demo-admin-secret
python3 server.py \
  --looker-base-url http://127.0.0.1:9999 \
  --entitlements entitlements.example.json \
  --debug-response &
curl -s -X POST http://127.0.0.1:8787/provision \
  -H 'Content-Type: application/json' -d '{"user_id": "101"}'
```

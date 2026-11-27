# Looker (Google Cloud core) Just-in-Time (JIT) Provisioning

**Language / Idioma:** English | [Español](README.es.md)

End-to-end reference implementation for eliminating the blank first-login experience on **Looker (Google Cloud core)** when fine-grained BI groups and row-level security (RLS) cannot be mirrored inside corporate SAML/OIDC IdP claims.

One unified codebase supports **two deployment profiles** tailored to segment size, networking, and governance requirements:

- **SMB / Mid-Market / Fast PoC (`smb`)**: A lightweight Looker Extension directly triggers a single Cloud Run service (`POST /provision`) that assigns Looker groups via API 4.0 and redirects the user straight to their Dashboard Hub in ~1–3 seconds.
- **Enterprise (`enterprise` / `gate` + `worker`)**: A **3-Plane Hybrid** combining a **Zero-Egress Passive Polling Extension** (`external_api_urls: []`), a **Private Event-Driven GCP Control Plane** (`Cloud Logging -> Pub/Sub -> Cloud Run Gate + Zero-PII Firestore Ledger -> Dual Priority Cloud Tasks -> Cloud Run Worker -> Direct VPC Egress -> PSC Internal ALB -> Looker API 4.0`), and a **Warehouse-Native BigQuery RLS Data Plane**.

---

## Segment Selection Matrix

| Criterion | SMB / Mid-Market (`smb`) | Enterprise (`enterprise`) |
| --- | --- | --- |
| **Target user base** | ~50 – 1,000 users | 1,000 – 50,000+ users |
| **Looker Core networking** | Public IP (or Public + Private) | **Private IP / PSC**, VPC-SC, CMEK |
| **Looker Extension egress** | `external_api_urls: ["https://..."]` (`fetchProxy` or `serverProxy`) | **`external_api_urls: []`** (zero external egress, zero CORS, zero PSC southbound) |
| **Provisioning trigger** | Synchronous `POST /provision` from Extension | Cloud Logging `user_login` -> Log Router Sink -> Pub/Sub -> Cloud Run Gate -> Cloud Tasks -> Worker |
| **Repeat-login overhead** | Short-lived in-memory replay cache | **Zero-PII Firestore Ledger** (~15 ms drop, 0 Looker API calls for repeat logins) |
| **Burst & revocation isolation** | Single Cloud Run service + semaphore | **Dual Cloud Tasks queues** (`jit-onboarding-high` for live logins, `reconcile-low` for IGA change-feed revocations) |
| **Entitlements source** | Local JSON file (`entitlements.example.json`) or simple DB | Enterprise IGA / HR REST API (`GET /entitlements`, `GET /changes`) |
| **High-cardinality RLS / CLS** | Optional Looker User Attributes (low cardinality) | **BigQuery clustered security table + LookML `sql_always_where`** joined on built-in SAML `{{ _user_attributes["email"] }}` (0 Looker API calls) |
| **Infrastructure footprint** | 1 Cloud Run service + Secret Manager | Pub/Sub (+ 7d DLT), Firestore, 2 Cloud Tasks queues, Cloud Scheduler, 2 internal Cloud Run services (Gate & Worker), Direct VPC Egress |
| **Typical setup time** | 30–60 minutes | 1–2 days (after Phase 0 checks) |

> **Upgrade path**: Start with `smb` in a dev/UAT project to validate Looker Core UX in under an hour, then switch `extension/src/config.js` from `mode: 'direct'` to `mode: 'passive_poll'` and deploy `infra/enterprise/`—no backend rewrite required.

---

## Repository Layout

```text
looker-core-jit-provisioning/
├── README.md                        # Primary documentation (English)
├── README.es.md                     # Documentación principal (Español)
├── backend/                         # Zero-dependency Python 3.12+ unified backend
│   ├── server.py                    # Multi-profile HTTP server (--profile smb|gate|worker|enterprise|all)
│   ├── provisioner.py               # Fail-closed delta provisioner, pending marker, safe prefix revocation
│   ├── looker_api.py                # Looker API 4.0 client (50m token TTL, semaphore, Retry-After)
│   ├── entitlements.py              # SMB JSON file & Enterprise IGA HTTP REST adapters
│   ├── ledger.py                    # Zero-PII InMemory & Firestore REST v1 CAS ledger
│   ├── task_dispatcher.py           # Inline (local) & Cloud Tasks REST v2 dual-queue dispatcher
│   ├── log_parser.py                # Pub/Sub push parser for Looker Core user_login logs
│   ├── gcp_auth.py                  # Metadata Server OAuth2 token client
│   ├── verify_immediacy.py          # Standalone CLI to validate Looker Core group-add behavior
│   ├── fake_looker.py               # Local mock Looker API 4.0 server for offline demos
│   ├── entitlements.example.json    # Example SMB entitlements mapping
│   ├── Dockerfile                   # Single container image for both SMB and Enterprise
│   └── test_*.py                    # Unit tests (Python stdlib unittest)
├── extension/                       # Dual-mode Looker Extension (passive_poll & direct)
│   ├── package.json                 # Webpack 5 + @looker/extension-sdk + polyfills
│   ├── webpack.config.cjs           # Bundle builder + iframe-safe dev server
│   ├── src/
│   │   ├── config.js                # Switch between 'passive_poll' (Enterprise) and 'direct' (SMB)
│   │   ├── flow.js                  # State machine: backoff polling, ?next= deep links, loop guard
│   │   ├── ui.js                    # Clean splash, ready, pending/no-access & retry screens
│   │   └── index.js                 # Extension SDK entrypoint
│   └── test/                        # Node built-in unit tests (`node --test`)
├── lookml/
│   ├── manifest.enterprise.lkml     # Zero-egress manifest (external_api_urls: [])
│   ├── manifest.smb.lkml            # Direct-trigger manifest (fetchProxy or serverProxy)
│   ├── provisioning_landing.model.lkml  # Empty model for Viewer-tier coarse SAML role
│   └── warehouse_rls.example.lkml   # Plane 3 LookML sql_always_where using SAML email
├── sql/
│   └── bigquery_rls_entitlements.sql # Plane 3 clustered BigQuery RLS table & merge template
├── infra/
│   ├── smb/                         # Terraform: 1 Cloud Run service + Secret Manager
│   └── enterprise/                  # Terraform: Log Sink, Pub/Sub + DLT, Firestore, Dual Cloud Tasks,
│                                    #            Cloud Scheduler, Internal Gate & Worker (Direct VPC)
└── docs/
    ├── ARCHITECTURE.md              # Deep dive: 3-plane flow, API call budget, IGA contract
    ├── CAVEATS.md                   # Production traps, license tiers, SAML settings, troubleshooting
    └── VALIDATION_CHECKLIST.md      # Phase 0 zero-code checks + SMB & Enterprise E2E sign-off
```

---

## Run the Offline Demo & Tests First (No GCP or Looker Required)

Both the **SMB** and **Enterprise** code paths run locally with zero external dependencies (Python 3.10+ and Node 20+):

### 1. Run Unit Tests

```bash
# Backend unit tests (Python stdlib)
cd backend && python3 -m unittest discover -p 'test_*.py' -v

# Extension unit tests (Node built-in test runner)
cd ../extension && node --test
```

### 2. Exercise Both Profiles Locally Against `fake_looker.py`

Open two terminals (or run the first two commands in the background):

```bash
# Terminal 1: Start mock Looker API 4.0 server on :9090
cd backend
python3 fake_looker.py --port 9090
```

```bash
# Terminal 2: Start unified backend in '--profile all' mode on :8787
cd backend
LOOKER_CLIENT_ID=demo-admin-id \
LOOKER_CLIENT_SECRET=demo-admin-secret \
PROVISIONING_SHARED_SECRET=dev-secret \
python3 server.py \
  --profile all \
  --looker-base-url http://127.0.0.1:9090 \
  --entitlements entitlements.example.json \
  --allowed-origin http://localhost:8080 \
  --excluded-actor-ids 1 \
  --pending-group BI_Access_Pending_Approval \
  --managed-group-prefix BI_ \
  --require-saml \
  --debug-response
```

```bash
# Terminal 3a — SMB Direct Trigger (POST /provision for Bob, user 102)
curl -sS -X POST http://127.0.0.1:8787/provision \
  -H 'Content-Type: application/json' \
  -H 'X-Provisioning-Secret: dev-secret' \
  -d '{"user_id":"102"}' | python3 -m json.tool

# Terminal 3b — Enterprise Gate (POST /events/user-login, Pub/Sub push for Alice, user 101)
MSG=$(printf '{"logName":"projects/demo/logs/looker.googleapis.com%%2Fuser_login","jsonPayload":{"eventName":"login","actor":{"id":"101","email":"alice@example.com"}}}' | base64 -w0)
curl -sS -X POST http://127.0.0.1:8787/events/user-login \
  -H 'Content-Type: application/json' \
  -d "{\"message\":{\"data\":\"$MSG\",\"messageId\":\"evt-1\"}}" | python3 -m json.tool

# Repeat login for Alice (101) -> Gate drops at zero-PII ledger with action="skip_fresh" (0 Looker calls)
curl -sS -X POST http://127.0.0.1:8787/events/user-login \
  -H 'Content-Type: application/json' \
  -d "{\"message\":{\"data\":\"$MSG\",\"messageId\":\"evt-2\"}}" | python3 -m json.tool

# Terminal 3c — Enterprise Reconciler (POST /tasks/reconcile for unentitled Carol, user 103)
curl -sS -X POST http://127.0.0.1:8787/tasks/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"emails":["carol@example.com"]}' | python3 -m json.tool
```

---

## Quick Start: SMB / Mid-Market Profile

1. **Run Phase 0 checks** in [docs/VALIDATION_CHECKLIST.md](docs/VALIDATION_CHECKLIST.md) (takes ~20 minutes on your dev Looker Core instance).
2. **Create the Viewer-tier coarse SAML role** in Looker:
   - Model Set: `provisioning_landing` only (see `lookml/provisioning_landing.model.lkml`).
   - Permission Set: **`access_data` ONLY** (do **not** grant `explore` or `develop`, which elevates license tier from Viewer to Standard/Developer).
   - Map your coarse SAML group (e.g., `SAML_BI_Users`) to this role, and set group-ranked `landing_page` to `/extensions/provisioning_landing::jit_onboarding` (lowest priority rank, while business groups have higher priority pointing to your Dashboard Hub).
3. **Build and deploy the backend**:
   ```bash
   # Build container and push to Artifact Registry
   docker build -t REGION-docker.pkg.dev/PROJECT/REPO/looker-jit:latest backend/
   docker push REGION-docker.pkg.dev/PROJECT/REPO/looker-jit:latest

   # Deploy Cloud Run + Secret Manager
   cd infra/smb
   cp terraform.tfvars.example terraform.tfvars
   # Edit terraform.tfvars, then populate the 3 Secret Manager secrets before apply
   terraform init && terraform validate && terraform plan
   terraform apply
   ```
   > Bake your real `entitlements.json` into the image or point `ENTITLEMENTS_PATH` / `IGA_BASE_URL` to your internal lookup store.
4. **Configure and bundle the Extension**:
   - In `extension/src/config.js`, set `mode: 'direct'`, `transport: 'server'`, `backendUrl: '<cloud_run_url>/provision'`, `secretKeyName: 'provisioning_secret'`, and keep `autoRedirect: false` until you verify which redirect mode (`reload` vs `spa` vs `logout`) refreshes permissions immediately on your instance.
   - Run `cd extension && npm install && npm run build`, then upload `dist/bundle.js` and `lookml/manifest.smb.lkml` (renamed to `manifest.lkml`) to your LookML project.

---

## Quick Start: Enterprise Profile (3-Plane Hybrid)

1. **Run Phase 0 checks** in [docs/VALIDATION_CHECKLIST.md](docs/VALIDATION_CHECKLIST.md) to confirm:
   - Exact `logName` emitted by Looker Core (`looker.googleapis.com%2Fuser_login` vs `looker.googleapis.com%2FUserLogin`).
   - Numeric Looker `user_id` of your dedicated Provisioning API Service Account (`provisioning_sa_looker_user_id`) so the Log Router Sink excludes it deterministically.
   - Group IDs for your coarse SAML group and zero-privilege `BI_Access_Pending_Approval` marker group.
2. **Prepare Looker Core groups & SAML settings**:
   - Create Viewer-tier coarse SAML role (`access_data` only on `provisioning_landing`), plus zero-privilege `BI_Access_Pending_Approval` group.
   - If **Mirror SAML Groups** is enabled, ensure **Prevent Direct Membership in non-SAML Groups** and **Prevent Role Inheritance from non-SAML Groups** are **OFF** (see [docs/CAVEATS.md](docs/CAVEATS.md)).
   - Prefix API-managed Looker groups consistently (default `BI_`) so delta revocation never touches `All Users` or IdP-mirrored groups.
3. **Deploy Plane 2 (Private Event-Driven Control Plane)**:
   ```bash
   docker build -t REGION-docker.pkg.dev/PROJECT/REPO/looker-jit:latest backend/
   docker push REGION-docker.pkg.dev/PROJECT/REPO/looker-jit:latest

   cd infra/enterprise
   cp terraform.tfvars.example terraform.tfvars
   # Fill project_id, looker_private_base_url (PSC Internal ALB), iga_base_url,
   # vpc_network, vpc_subnetwork, looker_login_log_name, provisioning_sa_looker_user_id.
   # Create Secret Manager versions for looker-client-id, looker-client-secret, iga-bearer-token.
   terraform init && terraform validate && terraform plan
   terraform apply
   ```
4. **Deploy Plane 1 (Zero-Egress Passive Polling Extension)**:
   - In `extension/src/config.js`, keep `mode: 'passive_poll'`, set `hubPath`, `ignoredGroupIds` (`['1', '<coarse_saml_group_id>']`), `pendingMarkerGroupId`, and optional `accessRequestUrl`.
   - Build (`cd extension && npm install && npm run build`) and deploy `dist/bundle.js` alongside `lookml/manifest.enterprise.lkml` (renamed to `manifest.lkml`, notice **`external_api_urls: []`**).
5. **Deploy Plane 3 (Warehouse-Native BigQuery RLS)**:
   - Provision the clustered table in `sql/bigquery_rls_entitlements.sql`, sync your IGA/HR feed into it, and attach the `sql_always_where` pattern from `lookml/warehouse_rls.example.lkml` to your production Explores.

---

## Documentation Index

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — End-to-end data flows, Admin API quota math, zero-PII Firestore ledger design, dual-queue sizing, and IGA REST API contract.
- [docs/CAVEATS.md](docs/CAVEATS.md) — License tier traps, Looker Core edition API limits, SAML Mirroring flags, Private IP/PSC networking, deep-link behavior, and troubleshooting table.
- [docs/VALIDATION_CHECKLIST.md](docs/VALIDATION_CHECKLIST.md) — Step-by-step Phase 0 verification plus SMB and Enterprise test matrices.

---

## Verification Scope

Before rolling out to production, run these environment-specific checks in your own workstation / GCP project:

- **Extension bundling**: Run `cd extension && npm install && npm run build` and confirm `dist/bundle.js` loads cleanly inside Looker Core.
- **Terraform plan**: Run `terraform init && terraform validate && terraform plan` in `infra/smb/` or `infra/enterprise/` against your target GCP project, VPC, and KMS keys.
- **Live Looker Core Phase 0 checks**: Execute all items in [docs/VALIDATION_CHECKLIST.md](docs/VALIDATION_CHECKLIST.md) using fresh throwaway SAML accounts (Looker Core disables `sudo` and `login_user` for standard accounts).

# Aprovisionamiento Just-in-Time (JIT) para Looker (Google Cloud core)

**Idioma / Language:** [English](README.md) | Español

Implementación de referencia de extremo a extremo para eliminar la pantalla en blanco en el primer inicio de sesión en **Looker (Google Cloud core)** cuando los grupos granulares de BI y la seguridad a nivel de fila (RLS) no pueden replicarse dentro de los claims del IdP corporativo (SAML/OIDC).

Un único código base soporta **dos perfiles de despliegue** adaptados al tamaño del segmento, arquitectura de red y requisitos de gobernanza:

- **SMB / Mid-Market / PoC rápido (`smb`)**: Una Looker Extension ligera invoca directamente a un único servicio de Cloud Run (`POST /provision`) que asigna los grupos de Looker vía API 4.0 y redirige al usuario a su Dashboard Hub en ~1–3 segundos.
- **Enterprise (`enterprise` / `gate` + `worker`)**: Una arquitectura **Híbrida de 3 Planos** que combina una **Extensión de Polling Pasivo sin Egreso** (`external_api_urls: []`), un **Plano de Control Privado Basado en Eventos en GCP** (`Cloud Logging -> Pub/Sub -> Cloud Run Gate + Ledger Firestore sin PII -> Colas Cloud Tasks con Doble Prioridad -> Cloud Run Worker -> Direct VPC Egress -> PSC Internal ALB -> Looker API 4.0`) y un **Plano de Datos RLS Nativo en el Warehouse (BigQuery)**.

---

## Matriz de Selección de Segmento

| Criterio | SMB / Mid-Market (`smb`) | Enterprise (`enterprise`) |
| --- | --- | --- |
| **Base de usuarios objetivo** | ~50 – 1,000 usuarios | 1,000 – 50,000+ usuarios |
| **Red de Looker Core** | IP Pública (o Pública + Privada) | **IP Privada / PSC**, VPC-SC, CMEK |
| **Egreso de la Looker Extension** | `external_api_urls: ["https://..."]` (`fetchProxy` o `serverProxy`) | **`external_api_urls: []`** (cero egreso externo, cero CORS, cero PSC southbound) |
| **Disparador de aprovisionamiento** | Llamada síncrona `POST /provision` desde la Extensión | Cloud Logging `user_login` -> Log Router Sink -> Pub/Sub -> Cloud Run Gate -> Cloud Tasks -> Worker |
| **Sobrecarga en logins repetidos** | Caché en memoria de corta duración | **Ledger Firestore sin PII** (~15 ms de descarte, 0 llamadas a la API de Looker en logins repetidos) |
| **Aislamiento de picos y revocaciones** | Un servicio Cloud Run + semáforo | **Colas Cloud Tasks con doble prioridad** (`jit-onboarding-high` para logins en vivo, `reconcile-low` para revocaciones delta del feed de cambios IGA) |
| **Fuente de permisos (entitlements)** | Archivo JSON local (`entitlements.example.json`) o BD sencilla | API REST de IGA / RR. HH. empresarial (`GET /entitlements`, `GET /changes`) |
| **RLS / CLS de alta cardinalidad** | Atributos de usuario de Looker opcionales (baja cardinalidad) | **Tabla de seguridad clusterizada en BigQuery + `sql_always_where` en LookML** unida por el atributo SAML nativo `{{ _user_attributes["email"] }}` (0 llamadas API) |
| **Huella de infraestructura** | 1 servicio Cloud Run + Secret Manager | Pub/Sub (+ DLT 7 días), Firestore, 2 colas Cloud Tasks, Cloud Scheduler, 2 servicios Cloud Run internos (Gate y Worker), Direct VPC Egress |
| **Tiempo típico de implementación** | 30–60 minutos | 1–2 días (tras las validaciones de Fase 0) |

> **Ruta de evolución**: Comienza con `smb` en un proyecto de desarrollo/UAT para validar la experiencia de usuario en Looker Core en menos de una hora; después cambia `extension/src/config.js` de `mode: 'direct'` a `mode: 'passive_poll'` y despliega `infra/enterprise/` sin reescribir el backend.

---

## Estructura del Repositorio

```text
looker-core-jit-provisioning/
├── README.md                        # Documentación principal (Inglés)
├── README.es.md                     # Documentación principal (Español)
├── backend/                         # Backend unificado en Python 3.12+ sin dependencias externas
│   ├── server.py                    # Servidor HTTP multi-perfil (--profile smb|gate|worker|enterprise|all)
│   ├── provisioner.py               # Aprovisionador delta fail-closed, marcador pendiente y revocación segura por prefijo
│   ├── looker_api.py                # Cliente Looker API 4.0 (TTL de token 50m, semáforo, Retry-After)
│   ├── entitlements.py              # Adaptadores para archivo JSON (SMB) y API REST IGA (Enterprise)
│   ├── ledger.py                    # Ledger sin PII en memoria y en Firestore REST v1 (CAS)
│   ├── task_dispatcher.py           # Despachador Inline (local) y Cloud Tasks REST v2 con doble cola
│   ├── log_parser.py                # Parser de Pub/Sub push para logs user_login de Looker Core
│   ├── gcp_auth.py                  # Cliente de tokens OAuth2 vía Metadata Server de GCP
│   ├── verify_immediacy.py          # CLI independiente para validar la propagación de grupos en Looker Core
│   ├── fake_looker.py               # Servidor mock local de Looker API 4.0 para demos offline
│   ├── entitlements.example.json    # Ejemplo de mapeo de permisos para SMB
│   ├── Dockerfile                   # Imagen de contenedor única para SMB y Enterprise
│   └── test_*.py                    # Pruebas unitarias (Python stdlib unittest)
├── extension/                       # Looker Extension dual (passive_poll y direct)
│   ├── package.json                 # Webpack 5 + @looker/extension-sdk + polyfills
│   ├── webpack.config.cjs           # Empaquetado y servidor dev compatible con iframe
│   ├── src/
│   │   ├── config.js                # Alterna entre 'passive_poll' (Enterprise) y 'direct' (SMB)
│   │   ├── flow.js                  # Máquina de estados: polling con backoff, deep links ?next=, protección anti-bucle
│   │   ├── ui.js                    # Pantallas de espera, listo, pendiente/sin acceso y reintento
│   │   └── index.js                 # Punto de entrada del Extension SDK
│   └── test/                        # Pruebas unitarias nativas de Node (`node --test`)
├── lookml/
│   ├── manifest.enterprise.lkml     # Manifiesto sin egreso (external_api_urls: [])
│   ├── manifest.smb.lkml            # Manifiesto directo (fetchProxy o serverProxy)
│   ├── provisioning_landing.model.lkml  # Modelo vacío para rol SAML inicial de nivel Viewer
│   └── warehouse_rls.example.lkml   # Plano 3: sql_always_where en LookML usando el email SAML
├── sql/
│   └── bigquery_rls_entitlements.sql # Plano 3: tabla RLS clusterizada en BigQuery y plantilla MERGE
├── infra/
│   ├── smb/                         # Terraform: 1 servicio Cloud Run + Secret Manager
│   └── enterprise/                  # Terraform: Log Sink, Pub/Sub + DLT, Firestore, Cloud Tasks dual,
│                                    #            Cloud Scheduler, Gate y Worker internos (Direct VPC)
└── docs/
    ├── ARCHITECTURE.md              # Profundización: flujo de 3 planos, presupuesto de API, contrato IGA
    ├── CAVEATS.md                   # Trampas en producción, licencias, ajustes SAML y solución de problemas
    └── VALIDATION_CHECKLIST.md      # Validaciones Fase 0 sin código + matrices E2E SMB y Enterprise
```

---

## Ejecuta Primero la Demo Offline y las Pruebas (Sin GCP ni Looker)

Tanto el flujo **SMB** como el **Enterprise** se ejecutan localmente sin dependencias externas (solo Python 3.10+ y Node 20+):

### 1. Ejecutar Pruebas Unitarias

```bash
# Pruebas unitarias del backend (Python stdlib)
cd backend && python3 -m unittest discover -p 'test_*.py' -v

# Pruebas unitarias de la extensión (ejecutor nativo de Node)
cd ../extension && node --test
```

### 2. Probar Ambos Perfiles Localmente Contra `fake_looker.py`

Abre terminales separadas (o ejecuta los dos primeros comandos en segundo plano):

```bash
# Terminal 1: Inicia el servidor mock de Looker API 4.0 en el puerto :9090
cd backend
python3 fake_looker.py --port 9090
```

```bash
# Terminal 2: Inicia el backend unificado en modo '--profile all' en el puerto :8787
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
# Terminal 3a — Disparador Directo SMB (POST /provision para Bob, usuario 102)
curl -sS -X POST http://127.0.0.1:8787/provision \
  -H 'Content-Type: application/json' \
  -H 'X-Provisioning-Secret: dev-secret' \
  -d '{"user_id":"102"}' | python3 -m json.tool

# Terminal 3b — Gate Enterprise (POST /events/user-login, sobre Pub/Sub push para Alice, usuario 101)
MSG=$(printf '{"logName":"projects/demo/logs/looker.googleapis.com%%2Fuser_login","jsonPayload":{"eventName":"login","actor":{"id":"101","email":"alice@example.com"}}}' | base64 -w0)
curl -sS -X POST http://127.0.0.1:8787/events/user-login \
  -H 'Content-Type: application/json' \
  -d "{\"message\":{\"data\":\"$MSG\",\"messageId\":\"evt-1\"}}" | python3 -m json.tool

# Login repetido para Alice (101) -> el Gate lo descarta en el ledger sin PII con action="skip_fresh" (0 llamadas a Looker)
curl -sS -X POST http://127.0.0.1:8787/events/user-login \
  -H 'Content-Type: application/json' \
  -d "{\"message\":{\"data\":\"$MSG\",\"messageId\":\"evt-2\"}}" | python3 -m json.tool

# Terminal 3c — Reconciliador Enterprise (POST /tasks/reconcile para Carol sin permisos, usuario 103)
curl -sS -X POST http://127.0.0.1:8787/tasks/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"emails":["carol@example.com"]}' | python3 -m json.tool
```

---

## Inicio Rápido: Perfil SMB / Mid-Market

1. **Ejecuta las validaciones de Fase 0** en [docs/VALIDATION_CHECKLIST.md](docs/VALIDATION_CHECKLIST.md) (~20 minutos en tu instancia de desarrollo de Looker Core).
2. **Crea el rol SAML inicial de nivel Viewer** en Looker:
   - Model Set: únicamente `provisioning_landing` (ver `lookml/provisioning_landing.model.lkml`).
   - Permission Set: **SOLO `access_data`** (**no** otorgues `explore` ni `develop`, ya que esto eleva la licencia de Viewer a Standard/Developer).
   - Mapea tu grupo SAML general (p. ej., `SAML_BI_Users`) a este rol y configura el atributo de usuario `landing_page` por rango de grupo hacia `/extensions/provisioning_landing::jit_onboarding` (con la prioridad más baja, mientras los grupos de negocio tienen mayor prioridad apuntando a tu Dashboard Hub).
3. **Construye y despliega el backend**:
   ```bash
   # Construye el contenedor y súbelo a Artifact Registry
   docker build -t REGION-docker.pkg.dev/PROJECT/REPO/looker-jit:latest backend/
   docker push REGION-docker.pkg.dev/PROJECT/REPO/looker-jit:latest

   # Despliega Cloud Run + Secret Manager
   cd infra/smb
   cp terraform.tfvars.example terraform.tfvars
   # Edita terraform.tfvars y crea las versiones de los 3 secretos en Secret Manager antes de aplicar
   terraform init && terraform validate && terraform plan
   terraform apply
   ```
   > Empaqueta tu `entitlements.json` real en la imagen o apunta `ENTITLEMENTS_PATH` / `IGA_BASE_URL` a tu almacén interno de permisos.
4. **Configura y empaqueta la Extensión**:
   - En `extension/src/config.js`, configura `mode: 'direct'`, `transport: 'server'`, `backendUrl: '<url_cloud_run>/provision'`, `secretKeyName: 'provisioning_secret'` y mantén `autoRedirect: false` hasta verificar qué modo de redirección (`reload`, `spa` o `logout`) refresca los permisos inmediatamente en tu instancia.
   - Ejecuta `cd extension && npm install && npm run build`, y sube `dist/bundle.js` junto con `lookml/manifest.smb.lkml` (renombrado a `manifest.lkml`) a tu proyecto LookML.

---

## Inicio Rápido: Perfil Enterprise (Híbrido de 3 Planos)

1. **Ejecuta las validaciones de Fase 0** en [docs/VALIDATION_CHECKLIST.md](docs/VALIDATION_CHECKLIST.md) para confirmar:
   - El `logName` exacto emitido por Looker Core (`looker.googleapis.com%2Fuser_login` vs `looker.googleapis.com%2FUserLogin`).
   - El `user_id` numérico en Looker de tu Service Account dedicada de aprovisionamiento (`provisioning_sa_looker_user_id`) para que el Log Router Sink la excluya de forma determinística.
   - Los IDs de grupo de tu grupo SAML general y del grupo marcador sin privilegios `BI_Access_Pending_Approval`.
2. **Prepara los grupos y ajustes SAML de Looker Core**:
   - Crea el rol SAML inicial de nivel Viewer (solo `access_data` en `provisioning_landing`) y el grupo sin privilegios `BI_Access_Pending_Approval`.
   - Si **Mirror SAML Groups** está activado, asegúrate de que **Prevent Direct Membership in non-SAML Groups** y **Prevent Role Inheritance from non-SAML Groups** estén **DESACTIVADOS** (ver [docs/CAVEATS.md](docs/CAVEATS.md)).
   - Usa un prefijo consistente en los grupos gestionados por API (por defecto `BI_`) para que la revocación delta nunca modifique `All Users` ni grupos sincronizados desde el IdP.
3. **Despliega el Plano 2 (Plano de Control Privado Basado en Eventos)**:
   ```bash
   docker build -t REGION-docker.pkg.dev/PROJECT/REPO/looker-jit:latest backend/
   docker push REGION-docker.pkg.dev/PROJECT/REPO/looker-jit:latest

   cd infra/enterprise
   cp terraform.tfvars.example terraform.tfvars
   # Completa project_id, looker_private_base_url (PSC Internal ALB), iga_base_url,
   # vpc_network, vpc_subnetwork, looker_login_log_name y provisioning_sa_looker_user_id.
   # Crea las versiones de secretos en Secret Manager para looker-client-id, looker-client-secret e iga-bearer-token.
   terraform init && terraform validate && terraform plan
   terraform apply
   ```
4. **Despliega el Plano 1 (Extensión de Polling Pasivo sin Egreso)**:
   - En `extension/src/config.js`, mantén `mode: 'passive_poll'` y define `hubPath`, `ignoredGroupIds` (`['1', '<id_grupo_saml_general>']`), `pendingMarkerGroupId` y, opcionalmente, `accessRequestUrl`.
   - Compila (`cd extension && npm install && npm run build`) y despliega `dist/bundle.js` junto con `lookml/manifest.enterprise.lkml` (renombrado a `manifest.lkml`; observa que define **`external_api_urls: []`**).
5. **Despliega el Plano 3 (RLS Nativo en BigQuery)**:
   - Crea la tabla clusterizada con `sql/bigquery_rls_entitlements.sql`, sincroniza tu feed de IGA/RR. HH. en ella y aplica el patrón `sql_always_where` de `lookml/warehouse_rls.example.lkml` en tus Explores productivos.

---

## Índice de Documentación

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — Flujos de datos completos, presupuesto de llamadas a la Admin API, diseño del ledger Firestore sin PII, dimensionamiento de colas duales y contrato REST de IGA.
- [docs/CAVEATS.md](docs/CAVEATS.md) — Trampas de licenciamiento, límites mensuales de Admin API en Looker Core, flags de SAML Mirroring, redes IP Privada/PSC, comportamiento de deep links y tabla de solución de problemas.
- [docs/VALIDATION_CHECKLIST.md](docs/VALIDATION_CHECKLIST.md) — Verificación paso a paso de Fase 0 y matrices de validación E2E para SMB y Enterprise.

---

## Alcance de Verificación

Antes de pasar a producción, ejecuta estas comprobaciones específicas en tu estación de trabajo y proyecto de GCP:

- **Empaquetado de la extensión**: Ejecuta `cd extension && npm install && npm run build` y confirma que `dist/bundle.js` carga correctamente dentro de Looker Core.
- **Planificación de Terraform**: Ejecuta `terraform init && terraform validate && terraform plan` en `infra/smb/` o `infra/enterprise/` contra tu proyecto GCP, VPC y claves KMS objetivo.
- **Validaciones en vivo de Fase 0 en Looker Core**: Ejecuta todos los puntos de [docs/VALIDATION_CHECKLIST.md](docs/VALIDATION_CHECKLIST.md) con cuentas SAML nuevas de prueba (Looker Core deshabilita `sudo` y `login_user` para cuentas estándar).

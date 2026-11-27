"""Unified HTTP server for both SMB and Enterprise Looker Core JIT profiles.

Endpoints enabled by --profile / DEPLOYMENT_PROFILE:
  - GET  /healthz            Liveness check (all profiles).
  - POST /provision          SMB / Direct Trigger from Looker Extension.
  - POST /events/user-login  Enterprise Gate: Pub/Sub push from Cloud Logging
                             -> zero-PII ledger check -> Cloud Tasks high queue.
  - POST /tasks/provision    Enterprise Worker: invoked by Cloud Tasks ->
                             fail-closed Looker API 4.0 delta writes.
  - POST /tasks/reconcile    Enterprise Reconciler: invoked by Cloud Scheduler ->
                             polls IGA change feed -> Cloud Tasks low queue.

Use `--profile all` (default) for local testing of both profiles in one process,
`--profile smb` for lightweight SMB deployments, or `--profile gate` /
`--profile worker` when splitting Enterprise Cloud Run Gate and Worker services.
"""

from __future__ import annotations

import argparse
import dataclasses
import hmac
import http.server
import json
import logging
import os
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

import entitlements as entitlements_lib
import ledger as ledger_lib
import log_parser
import looker_api
import provisioner as provisioner_lib
import task_dispatcher

_MAX_BODY_BYTES = 64 * 1024  # Allows Cloud Logging Pub/Sub push envelopes.
_USER_ID_RE = re.compile(r"[0-9]{1,20}")
_SECRET_HEADER = "X-Provisioning-Secret"
_CACHEABLE_STATUSES = frozenset({
    provisioner_lib.STATUS_PROVISIONED,
    provisioner_lib.STATUS_ALREADY_PROVISIONED,
})


@dataclasses.dataclass
class App:
    """Shared application state and handlers across all profiles."""

    provisioner: Any
    api: Optional[looker_api.LookerApi] = None
    entitlements: Any = None
    ledger: Any = None
    dispatcher: Any = None
    profile: str = "all"  # 'smb', 'gate', 'worker', 'enterprise', or 'all'
    allowed_origins: FrozenSet[str] = frozenset()
    shared_secret: Optional[str] = None
    excluded_actor_ids: FrozenSet[str] = frozenset()
    fresh_ttl_s: float = 86400.0
    lease_ttl_s: float = 60.0
    debug_response: bool = False
    cache_ttl_s: float = 300.0
    clock: Callable[[], float] = time.monotonic
    _recent: Dict[str, Tuple[float, Dict[str, Any]]] = dataclasses.field(
        default_factory=dict, init=False, repr=False
    )
    _lock: threading.Lock = dataclasses.field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def allows_route(self, path: str) -> bool:
        if path == "/healthz":
            return True
        if self.profile == "all":
            return path in (
                "/provision",
                "/events/user-login",
                "/tasks/provision",
                "/tasks/reconcile",
            )
        if self.profile == "smb":
            return path == "/provision"
        if self.profile == "gate":
            return path in ("/events/user-login", "/tasks/reconcile")
        if self.profile == "worker":
            return path == "/tasks/provision"
        if self.profile == "enterprise":
            return path in (
                "/events/user-login",
                "/tasks/provision",
                "/tasks/reconcile",
            )
        return False

    def provision_direct(self, user_id: str) -> Dict[str, Any]:
        """SMB direct-trigger path with short-lived in-memory replay cache."""
        now = self.clock()
        with self._lock:
            cached = self._recent.get(user_id)
            if cached and cached[0] > now:
                return dict(
                    cached[1], cached=True, admin_api_calls=0, elapsed_ms=0
                )
        result = self.provisioner.provision(user_id)
        if self.cache_ttl_s > 0 and result["status"] in _CACHEABLE_STATUSES:
            with self._lock:
                self._recent[user_id] = (now + self.cache_ttl_s, result)
                for key in [k for k, v in self._recent.items() if v[0] <= now]:
                    del self._recent[key]
        return result

    def handle_login_event(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """Enterprise Gate: Pub/Sub push -> zero-PII ledger -> High Queue."""
        event = log_parser.parse_pubsub_push(
            envelope, excluded_actor_ids=self.excluded_actor_ids
        )
        if event.ignored_reason:
            return {
                "action": "ignored",
                "reason": event.ignored_reason,
                "user_id": event.user_id or None,
            }
        decision = ledger_lib.DECISION_ENQUEUE
        if self.ledger is not None:
            decision = self.ledger.try_acquire_lease(
                event.user_id,
                fresh_ttl_s=self.fresh_ttl_s,
                lease_ttl_s=self.lease_ttl_s,
            )
        if decision != ledger_lib.DECISION_ENQUEUE:
            return {
                "action": decision,
                "user_id": event.user_id,
                "event_id": event.event_id,
            }
        try:
            dispatch_result = self.dispatcher.enqueue(
                {"user_id": event.user_id, "revoke": False, "source": "login"},
                priority=task_dispatcher.PRIORITY_HIGH,
            )
        except Exception:
            if self.ledger is not None:
                self.ledger.release_lease(event.user_id)
            raise
        return {
            "action": "enqueued",
            "user_id": event.user_id,
            "event_id": event.event_id,
            "dispatch": dispatch_result,
        }

    def handle_reconcile(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Enterprise Reconciler: IGA change feed -> Low Queue (no 10K scan)."""
        emails: List[str] = []
        next_cursor: Optional[str] = None
        if isinstance(payload.get("emails"), list):
            emails = [
                str(e).strip().lower()
                for e in payload["emails"]
                if str(e).strip()
            ]
        elif self.entitlements is not None and hasattr(
            self.entitlements, "changed_emails"
        ):
            since = payload.get("since")
            if since is None and self.ledger is not None:
                since = self.ledger.get_reconcile_cursor()
            emails, next_cursor = self.entitlements.changed_emails(since=since)

        enqueued = 0
        not_found = 0
        for email in emails:
            users = self.api.search_users_by_email(email) if self.api else []
            matches = [
                u
                for u in (users or [])
                if (u.get("email") or "").strip().lower() == email
            ]
            if not matches:
                not_found += 1
                continue
            for match in matches:
                uid = str(match["id"])
                self.dispatcher.enqueue(
                    {"user_id": uid, "revoke": True, "source": "reconcile"},
                    priority=task_dispatcher.PRIORITY_LOW,
                )
                enqueued += 1
        if next_cursor and self.ledger is not None:
            self.ledger.set_reconcile_cursor(next_cursor)
        return {
            "status": "ok",
            "changed_emails": len(emails),
            "enqueued": enqueued,
            "not_found_in_looker": not_found,
            "next_cursor": next_cursor,
        }


class ProvisioningServer(http.server.ThreadingHTTPServer):
    """HTTP server carrying the shared App."""

    daemon_threads = True

    def __init__(self, address: Tuple[str, int], app: App):
        super().__init__(address, _Handler)
        self.app = app


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LookerJitProvisioning/1.0"
    server: ProvisioningServer

    def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
        logging.debug("%s %s", self.address_string(), format % args)

    def do_OPTIONS(self) -> None:  # pylint: disable=invalid-name
        allowed, origin = self._check_origin()
        if not allowed or origin is None:
            self._send_json(403, {"status": "error", "message": "Forbidden."})
            return
        headers = {
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": f"Content-Type, {_SECRET_HEADER}",
            "Access-Control-Max-Age": "600",
        }
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            headers["Access-Control-Allow-Private-Network"] = "true"
        self._send_json(204, None, origin=origin, extra_headers=headers)

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        if self.path == "/healthz":
            self._send_json(
                200, {"status": "ok", "profile": self.server.app.profile}
            )
        else:
            self._send_json(404, {"status": "error", "message": "Not found."})

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        app = self.server.app
        allowed, origin = self._check_origin()
        payload = self._read_json()
        if not app.allows_route(self.path):
            self._send_json(404, {"status": "error", "message": "Not found."})
            return
        if self.path == "/provision":
            self._handle_smb_provision(app, allowed, origin, payload)
        elif self.path == "/events/user-login":
            self._handle_gate_event(app, payload)
        elif self.path == "/tasks/provision":
            self._handle_worker_task(app, payload)
        elif self.path == "/tasks/reconcile":
            self._handle_reconcile_task(app, payload)
        else:
            self._send_json(404, {"status": "error", "message": "Not found."})

    def _handle_smb_provision(
        self, app: App, allowed: bool, origin: Optional[str], payload: Any
    ) -> None:
        if not allowed:
            logging.warning(
                "Rejected Origin %r; pass --allowed-origin to accept it",
                self.headers.get("Origin"),
            )
            self._send_json(403, {"status": "error", "message": "Forbidden."})
            return
        if isinstance(payload, int):
            self._send_json(
                payload,
                {"status": "error", "message": "Invalid request body."},
                origin=origin,
            )
            return
        if app.shared_secret and not self._secret_matches(payload):
            logging.warning("Rejected /provision call without valid secret")
            self._send_json(
                401,
                {"status": "error", "message": "Unauthorized."},
                origin=origin,
            )
            return
        user_id = _extract_user_id(payload)
        if user_id is None:
            self._send_json(
                400,
                {"status": "error", "message": "Invalid user_id."},
                origin=origin,
            )
            return
        try:
            result = app.provision_direct(user_id)
        except provisioner_lib.ConfigError as error:
            logging.error("user=%s misconfiguration: %s", user_id, error)
            self._send_json(
                500,
                {"status": "error", "message": "Provisioning is misconfigured."},
                origin=origin,
            )
            return
        except (
            looker_api.LookerApiError,
            entitlements_lib.EntitlementsError,
        ) as error:
            logging.error("user=%s upstream failure: %s", user_id, error)
            self._send_json(
                502,
                {"status": "error", "message": "Upstream API call failed."},
                origin=origin,
            )
            return
        logging.info(
            "route=/provision user=%s origin=%s %s",
            user_id,
            origin,
            json.dumps(result, sort_keys=True),
        )
        body = (
            result
            if app.debug_response
            else {"status": result["status"], "redirect": result["redirect"]}
        )
        self._send_json(200, body, origin=origin)

    def _handle_gate_event(self, app: App, payload: Any) -> None:
        if isinstance(payload, int):
            self._send_json(
                payload, {"status": "error", "message": "Invalid Pub/Sub body."}
            )
            return
        try:
            outcome = app.handle_login_event(payload)
        except log_parser.LogParseError as error:
            # Return 200 on permanently malformed messages? No: 400 routes bad
            # payloads to Pub/Sub Dead-Letter Topic for operator visibility.
            logging.warning("Invalid Pub/Sub login payload: %s", error)
            self._send_json(400, {"status": "error", "message": str(error)})
            return
        except (ledger_lib.LedgerError, task_dispatcher.DispatchError) as error:
            logging.error("Gate infrastructure error: %s", error)
            self._send_json(
                503, {"status": "error", "message": "Transient gate failure."}
            )
            return
        logging.info(
            "route=/events/user-login %s", json.dumps(outcome, sort_keys=True)
        )
        self._send_json(200, outcome)

    def _handle_worker_task(self, app: App, payload: Any) -> None:
        if isinstance(payload, int):
            self._send_json(
                payload, {"status": "error", "message": "Invalid task body."}
            )
            return
        user_id = _extract_user_id(payload)
        if user_id is None:
            self._send_json(
                400, {"status": "error", "message": "Invalid user_id."}
            )
            return
        revoke = bool(payload.get("revoke", False))
        try:
            result = app.provisioner.provision(user_id, revoke=revoke)
        except provisioner_lib.ConfigError as error:
            logging.error("worker user=%s config error: %s", user_id, error)
            if app.ledger is not None:
                app.ledger.release_lease(user_id)
            self._send_json(
                500,
                {"status": "error", "message": "Provisioning is misconfigured."},
            )
            return
        except (
            looker_api.LookerApiError,
            entitlements_lib.EntitlementsError,
        ) as error:
            logging.error("worker user=%s transient error: %s", user_id, error)
            if app.ledger is not None:
                app.ledger.release_lease(user_id)
            # 503 triggers Cloud Tasks exponential retry.
            self._send_json(
                503, {"status": "error", "message": "Upstream API failure."}
            )
            return
        logging.info(
            "route=/tasks/provision user=%s revoke=%s %s",
            user_id,
            revoke,
            json.dumps(result, sort_keys=True),
        )
        self._send_json(200, result)

    def _handle_reconcile_task(self, app: App, payload: Any) -> None:
        if isinstance(payload, int):
            if payload == 400 and not self.headers.get("Content-Length"):
                payload = {}
            else:
                self._send_json(
                    payload,
                    {"status": "error", "message": "Invalid reconcile body."},
                )
                return
        try:
            outcome = app.handle_reconcile(payload)
        except (
            looker_api.LookerApiError,
            entitlements_lib.EntitlementsError,
            ledger_lib.LedgerError,
            task_dispatcher.DispatchError,
        ) as error:
            logging.error("reconcile error: %s", error)
            self._send_json(
                503, {"status": "error", "message": "Reconciliation failed."}
            )
            return
        logging.info(
            "route=/tasks/reconcile %s", json.dumps(outcome, sort_keys=True)
        )
        self._send_json(200, outcome)

    def _check_origin(self) -> Tuple[bool, Optional[str]]:
        origin = self.headers.get("Origin")
        if origin is None:
            return True, None
        allowed_origins = self.server.app.allowed_origins
        if "*" in allowed_origins or origin in allowed_origins:
            return True, origin
        return False, None

    def _read_json(self) -> Any:
        transfer = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer:
            raw = self._read_chunked()
            if isinstance(raw, int):
                return raw
        else:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self.close_connection = True
                return 400
            if length > _MAX_BODY_BYTES:
                self.close_connection = True
                return 413
            raw = self.rfile.read(length) if length else b""
        if not raw:
            return 400
        try:
            payload = json.loads(raw)
        except ValueError:
            return 400
        return payload if isinstance(payload, dict) else 400

    def _read_chunked(self) -> Any:
        chunks = bytearray()
        while True:
            line = self.rfile.readline(_MAX_BODY_BYTES + 2)
            if not line:
                self.close_connection = True
                return 400
            size_token = line.split(b";", 1)[0].strip()
            try:
                size = int(size_token, 16)
            except ValueError:
                self.close_connection = True
                return 400
            if size < 0:
                self.close_connection = True
                return 400
            if size == 0:
                while True:
                    trailer = self.rfile.readline(_MAX_BODY_BYTES + 2)
                    if trailer in (b"", b"\r\n", b"\n"):
                        break
                return bytes(chunks)
            if len(chunks) + size > _MAX_BODY_BYTES:
                self.close_connection = True
                return 413
            chunk = self.rfile.read(size)
            if len(chunk) != size:
                self.close_connection = True
                return 400
            chunks.extend(chunk)
            crlf = self.rfile.read(2)
            if crlf not in (b"\r\n", b"\n"):
                self.close_connection = True
                return 400

    def _secret_matches(self, payload: Dict[str, Any]) -> bool:
        provided = self.headers.get(_SECRET_HEADER) or payload.get("secret")
        expected = self.server.app.shared_secret or ""
        return isinstance(provided, str) and hmac.compare_digest(
            provided.encode("utf-8"), expected.encode("utf-8")
        )

    def _send_json(
        self,
        status: int,
        payload: Any,
        *,
        origin: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        data = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Origin")
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)


def _extract_user_id(payload: Dict[str, Any]) -> Optional[str]:
    user_id = payload.get("user_id")
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        user_id = str(user_id)
    if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
        return None
    return user_id


def _normalize_origins(cli_origins: Sequence[str]) -> FrozenSet[str]:
    env_raw = os.environ.get("ALLOWED_ORIGINS", "")
    env_origins = [item.strip() for item in env_raw.split(",") if item.strip()]
    normalized = set()
    for origin in [*cli_origins, *env_origins]:
        cleaned = origin.strip()
        if cleaned == "*":
            normalized.add("*")
        elif cleaned:
            normalized.add(cleaned.rstrip("/"))
    return frozenset(normalized)


def _csv_set(raw: Optional[str]) -> FrozenSet[str]:
    if not raw:
        return frozenset()
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--profile",
        choices=["smb", "gate", "worker", "enterprise", "all"],
        default=os.environ.get("DEPLOYMENT_PROFILE", "all"),
        help="Active HTTP endpoints profile.",
    )
    parser.add_argument(
        "--looker-base-url",
        default=os.environ.get("LOOKER_BASE_URL"),
        help="Looker instance URL (required unless --profile=gate).",
    )
    parser.add_argument(
        "--entitlements",
        default=os.environ.get("ENTITLEMENTS_PATH"),
        help="Path to JSON entitlements file (SMB / PoC).",
    )
    parser.add_argument(
        "--iga-base-url",
        default=os.environ.get("IGA_BASE_URL"),
        help="Enterprise IGA REST API base URL (alternative to --entitlements).",
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        help="Allowed browser origin for SMB /provision CORS (repeatable).",
    )
    parser.add_argument(
        "--bind", default=os.environ.get("BIND_ADDRESS", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", "8787"))
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-landing-page",
        action="store_true",
        default=os.environ.get("NO_LANDING_PAGE", "").lower() in ("1", "true"),
        help="Skip per-user landing_page write (use group-ranked landing_page).",
    )
    parser.add_argument(
        "--require-saml",
        action="store_true",
        default=os.environ.get("REQUIRE_SAML", "").lower() in ("1", "true"),
    )
    parser.add_argument(
        "--pending-group",
        default=os.environ.get("PENDING_MARKER_GROUP") or None,
        help="Zero-privilege marker group assigned when user has no entitlements.",
    )
    parser.add_argument(
        "--managed-group-prefix",
        default=os.environ.get("MANAGED_GROUP_PREFIX") or None,
        help="Group-name prefix eligible for safe delta revocation (e.g. BI_).",
    )
    parser.add_argument(
        "--managed-groups",
        default=os.environ.get("MANAGED_GROUPS") or "",
        help="Comma-separated exact group names eligible for delta revocation.",
    )
    parser.add_argument(
        "--excluded-actor-ids",
        default=os.environ.get("EXCLUDED_ACTOR_IDS") or "",
        help="Comma-separated Looker SA user IDs excluded from /events/user-login.",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=int(os.environ.get("LOOKER_MAX_CONCURRENCY", "10")),
    )
    parser.add_argument(
        "--fresh-ttl-s",
        type=float,
        default=float(os.environ.get("LEDGER_FRESH_TTL_S", "86400")),
    )
    parser.add_argument(
        "--lease-ttl-s",
        type=float,
        default=float(os.environ.get("LEDGER_LEASE_TTL_S", "60")),
    )
    parser.add_argument("--debug-response", action="store_true")
    parser.add_argument("--cache-ttl-s", type=float, default=300.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    needs_looker = args.profile != "gate"
    if needs_looker and not args.looker_base_url:
        parser.error("--looker-base-url or LOOKER_BASE_URL is required")
    if needs_looker and not (args.entitlements or args.iga_base_url):
        parser.error("Provide --entitlements or --iga-base-url")
    return args


def _build_app(args: argparse.Namespace) -> App:
    gcp_project = os.environ.get("GCP_PROJECT_ID")
    firestore_db = os.environ.get("FIRESTORE_DATABASE")
    if gcp_project and firestore_db:
        ledger: Any = ledger_lib.FirestoreRestLedger(
            gcp_project,
            database=firestore_db,
            collection=os.environ.get(
                "FIRESTORE_COLLECTION", "looker_jit_ledger"
            ),
        )
    else:
        ledger = ledger_lib.InMemoryLedger()

    entitlements_source: Any = None
    if args.iga_base_url:
        entitlements_source = entitlements_lib.HttpIgaEntitlements(
            args.iga_base_url,
            bearer_token=os.environ.get("IGA_BEARER_TOKEN") or None,
        )
    elif args.entitlements:
        entitlements_source = entitlements_lib.EntitlementsFile(args.entitlements)

    api: Optional[looker_api.LookerApi] = None
    provisioner: Any = None
    if args.looker_base_url:
        client_id = os.environ.get("LOOKER_CLIENT_ID")
        client_secret = os.environ.get("LOOKER_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise SystemExit(
                "Set LOOKER_CLIENT_ID and LOOKER_CLIENT_SECRET for Looker API."
            )
        api = looker_api.LookerApi(
            args.looker_base_url,
            client_id,
            client_secret,
            max_concurrency=args.max_concurrency,
        )
        provisioner = provisioner_lib.Provisioner(
            api,
            entitlements_source,
            set_landing_page=not args.no_landing_page,
            require_saml=args.require_saml,
            pending_group_name=args.pending_group,
            managed_group_prefix=args.managed_group_prefix,
            managed_groups=_csv_set(args.managed_groups),
            ledger=ledger,
            dry_run=args.dry_run,
        )

    worker_url = os.environ.get("CLOUD_TASKS_WORKER_URL")
    if gcp_project and worker_url:
        dispatcher: Any = task_dispatcher.CloudTasksRestDispatcher(
            gcp_project,
            os.environ.get("CLOUD_TASKS_LOCATION", "europe-west3"),
            os.environ.get("CLOUD_TASKS_HIGH_QUEUE", "jit-onboarding-high"),
            os.environ.get("CLOUD_TASKS_LOW_QUEUE", "reconcile-low"),
            worker_url,
            oidc_service_account=os.environ.get("CLOUD_TASKS_OIDC_SA") or None,
            oidc_audience=os.environ.get("CLOUD_TASKS_OIDC_AUDIENCE") or None,
        )
    else:
        dispatcher = task_dispatcher.InlineDispatcher(
            lambda task: provisioner.provision(
                task["user_id"], revoke=bool(task.get("revoke", False))
            )
            if provisioner is not None
            else {"status": "noop"}
        )

    return App(
        provisioner=provisioner,
        api=api,
        entitlements=entitlements_source,
        ledger=ledger,
        dispatcher=dispatcher,
        profile=args.profile,
        allowed_origins=_normalize_origins(args.allowed_origin),
        shared_secret=os.environ.get("PROVISIONING_SHARED_SECRET") or None,
        excluded_actor_ids=_csv_set(args.excluded_actor_ids),
        fresh_ttl_s=args.fresh_ttl_s,
        lease_ttl_s=args.lease_ttl_s,
        debug_response=args.debug_response,
        cache_ttl_s=args.cache_ttl_s,
    )


def main(argv: Sequence[str]) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    app = _build_app(args)
    httpd = ProvisioningServer((args.bind, args.port), app)
    logging.info(
        "Listening on http://%s:%d (profile=%s, dry_run=%s, ledger=%s, "
        "dispatcher=%s)",
        args.bind,
        httpd.server_address[1],
        app.profile,
        args.dry_run,
        type(app.ledger).__name__,
        type(app.dispatcher).__name__,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

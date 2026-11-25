"""HTTP front end of the provisioning backend called by the landing extension.

Exposes one endpoint:

    POST /provision   {"user_id": "<Looker user ID>", "secret": "<optional>"}

and answers {"status": ..., "redirect": ...}. The Looker user ID is the only
input taken from the caller; the email and the entitlements are looked up
server side, so a forged ID can at most trigger the correct provisioning of
another user.

Run it with nothing but Python 3.9+:

    export LOOKER_CLIENT_ID=... LOOKER_CLIENT_SECRET=...
    python3 server.py --looker-base-url https://example.cloud.looker.com \
        --entitlements entitlements.json \
        --allowed-origin https://example.cloud.looker.com
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
from typing import Any, Callable, Dict, FrozenSet, Optional, Sequence, Tuple

import looker_api
import provisioner as provisioner_lib

_MAX_BODY_BYTES = 10 * 1024
_USER_ID_RE = re.compile(r"[0-9]{1,20}")
_SECRET_HEADER = "X-Provisioning-Secret"
_CACHEABLE_STATUSES = frozenset({
    provisioner_lib.STATUS_PROVISIONED,
    provisioner_lib.STATUS_ALREADY_PROVISIONED,
})


@dataclasses.dataclass
class App:
    """Configuration and state shared by all requests.

    Attributes:
        provisioner: Object with a provision(user_id) -> dict method.
        allowed_origins: Browser origins allowed to call the endpoint, or
            {"*"} for any. Requests without an Origin header (server-to-server
            calls such as the Looker server proxy) are not subject to it.
        shared_secret: When set, callers must present it.
        debug_response: Whether to return the provisioning details to the
            caller instead of only the status and the redirect.
        cache_ttl_s: How long a successful result is replayed without calling
            Looker again. Shields the admin API allotment from repeated calls.
        clock: Monotonic clock, injectable for tests.
    """

    provisioner: Any
    allowed_origins: FrozenSet[str] = frozenset()
    shared_secret: Optional[str] = None
    debug_response: bool = False
    cache_ttl_s: float = 300.0
    clock: Callable[[], float] = time.monotonic
    _recent: Dict[str, Tuple[float, Dict[str, Any]]] = dataclasses.field(
        default_factory=dict, init=False, repr=False
    )
    _lock: threading.Lock = dataclasses.field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def provision(self, user_id: str) -> Dict[str, Any]:
        """Provisions a user, replaying a recent successful result if any."""
        now = self.clock()
        with self._lock:
            cached = self._recent.get(user_id)
            if cached and cached[0] > now:
                # A replay costs no Looker call; report it as such.
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


class ProvisioningServer(http.server.ThreadingHTTPServer):
    """HTTP server carrying the App the handlers work with."""

    daemon_threads = True

    def __init__(self, address: Tuple[str, int], app: App):
        super().__init__(address, _Handler)
        self.app = app


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ProvisioningPoC/0.1"
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
            # Lets a public page (the Looker UI) call a backend on localhost
            # or on a private network from the browser.
            headers["Access-Control-Allow-Private-Network"] = "true"
        self._send_json(204, None, origin=origin, extra_headers=headers)

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"status": "error", "message": "Not found."})

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        app = self.server.app
        allowed, origin = self._check_origin()
        payload = self._read_json()
        if self.path != "/provision":
            self._send_json(404, {"status": "error", "message": "Not found."})
            return
        if not allowed:
            logging.warning(
                "Rejected Origin %r; pass --allowed-origin to accept it",
                self.headers.get("Origin"),
            )
            self._send_json(403, {"status": "error", "message": "Forbidden."})
            return
        if isinstance(payload, int):  # An HTTP error status from _read_json.
            self._send_json(
                payload,
                {"status": "error", "message": "Invalid request body."},
                origin=origin,
            )
            return
        if app.shared_secret and not self._secret_matches(payload):
            logging.warning(
                "Rejected a call without the shared secret (a literal "
                "'{{...}}' means Looker did not resolve the secret key tag)"
            )
            self._send_json(
                401,
                {"status": "error", "message": "Unauthorized."},
                origin=origin,
            )
            return
        user_id = payload.get("user_id")
        if isinstance(user_id, int) and not isinstance(user_id, bool):
            user_id = str(user_id)
        if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
            self._send_json(
                400,
                {"status": "error", "message": "Invalid user_id."},
                origin=origin,
            )
            return

        try:
            result = app.provision(user_id)
        except provisioner_lib.ConfigError as error:
            logging.error("user=%s misconfiguration: %s", user_id, error)
            self._send_json(
                500,
                {"status": "error", "message": "Provisioning is misconfigured."},
                origin=origin,
            )
            return
        except looker_api.LookerApiError as error:
            logging.error("user=%s Looker API failure: %s", user_id, error)
            self._send_json(
                502,
                {"status": "error", "message": "Looker API call failed."},
                origin=origin,
            )
            return
        logging.info(
            "user=%s origin=%s %s",
            user_id,
            origin,
            json.dumps(result, sort_keys=True),
        )
        # Only the status and the redirect leave the server by default.
        body = (
            result
            if app.debug_response
            else {"status": result["status"], "redirect": result["redirect"]}
        )
        self._send_json(200, body, origin=origin)

    def _check_origin(self) -> Tuple[bool, Optional[str]]:
        """Returns (allowed, origin to echo in the CORS headers)."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True, None
        allowed_origins = self.server.app.allowed_origins
        if "*" in allowed_origins or origin in allowed_origins:
            return True, origin
        return False, None

    def _read_json(self) -> Any:
        """Returns the JSON object of the body, or an HTTP error status."""
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
                self.close_connection = True  # The unread body would follow.
                return 413
            raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw)
        except ValueError:
            return 400
        return payload if isinstance(payload, dict) else 400

    def _read_chunked(self) -> Any:
        """Decodes an HTTP/1.1 chunked body up to _MAX_BODY_BYTES."""
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
                # Consume trailer lines through the terminating blank line.
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


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--looker-base-url",
        default=os.environ.get("LOOKER_BASE_URL"),
        help="Looker instance URL (or set LOOKER_BASE_URL).",
    )
    parser.add_argument(
        "--entitlements",
        default=os.environ.get("ENTITLEMENTS_PATH"),
        help=(
            "JSON file of entitlements (or set ENTITLEMENTS_PATH); "
            "see entitlements.example.json."
        ),
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        help=(
            "Browser origin allowed to call the endpoint, normally the Looker "
            "instance URL (or comma-separated ALLOWED_ORIGINS). Repeatable; "
            "'*' allows any origin (testing only)."
        ),
    )
    parser.add_argument(
        "--bind",
        default=os.environ.get("BIND_ADDRESS", "127.0.0.1"),
        help="Address to bind (use 0.0.0.0 in containers).",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", "8787"))
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be provisioned without writing to Looker.",
    )
    parser.add_argument(
        "--no-landing-page",
        action="store_true",
        help=(
            "Don't store the landing page on the user; only return it for the "
            "redirect. Saves one admin API call per user."
        ),
    )
    parser.add_argument(
        "--require-saml",
        action="store_true",
        default=os.environ.get("REQUIRE_SAML", "").lower() in ("1", "true"),
        help="Skip accounts that do not have active SAML credentials.",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=int(os.environ.get("LOOKER_MAX_CONCURRENCY", "10")),
        help="Maximum simultaneous in-flight Looker API calls.",
    )
    parser.add_argument(
        "--debug-response",
        action="store_true",
        help="Return the provisioning details to the caller (testing only).",
    )
    parser.add_argument(
        "--cache-ttl-s",
        type=float,
        default=300.0,
        help="Seconds a successful result is replayed without calling Looker.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not args.looker_base_url:
        parser.error("--looker-base-url or LOOKER_BASE_URL is required")
    if not args.entitlements:
        parser.error("--entitlements or ENTITLEMENTS_PATH is required")
    return args


def _normalize_origins(cli_origins: Sequence[str]) -> FrozenSet[str]:
    """Combines CLI and env origins and strips trailing slashes."""
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


def main(argv: Sequence[str]) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    client_id = os.environ.get("LOOKER_CLIENT_ID")
    client_secret = os.environ.get("LOOKER_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "Set LOOKER_CLIENT_ID and LOOKER_CLIENT_SECRET to the API key of "
            "a Looker admin (service) account.",
            file=sys.stderr,
        )
        return 2
    api = looker_api.LookerApi(
        args.looker_base_url,
        client_id,
        client_secret,
        max_concurrency=args.max_concurrency,
    )
    app = App(
        provisioner=provisioner_lib.Provisioner(
            api,
            provisioner_lib.EntitlementsFile(args.entitlements),
            set_landing_page=not args.no_landing_page,
            require_saml=args.require_saml,
            dry_run=args.dry_run,
        ),
        allowed_origins=_normalize_origins(args.allowed_origin),
        shared_secret=os.environ.get("PROVISIONING_SHARED_SECRET") or None,
        debug_response=args.debug_response,
        cache_ttl_s=args.cache_ttl_s,
    )
    httpd = ProvisioningServer((args.bind, args.port), app)
    logging.info(
        "Listening on http://%s:%d (Looker: %s, dry run: %s, shared secret: "
        "%s, allowed origins: %s)",
        args.bind,
        httpd.server_address[1],
        args.looker_base_url,
        args.dry_run,
        "required" if app.shared_secret else "not required",
        sorted(app.allowed_origins) or "none (server-to-server calls only)",
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

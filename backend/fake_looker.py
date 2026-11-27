"""In-memory fake of the Looker API 4.0 endpoints used by this repository.

For unit tests and offline demos only. Not evidence of live Looker behavior.
"""

from __future__ import annotations

import argparse
import http.server
import itertools
import json
import re
import sys
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

API_PREFIX = "/api/4.0"
_NOT_FOUND = (404, {"message": "Not found"})


class FakeClock:
    """Deterministic clock advanced only by sleep()."""

    def __init__(self, start: float = 1000.0):
        self._now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


def _like_to_regex(pattern: str) -> "re.Pattern[str]":
    parts = []
    for char in pattern:
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts), re.IGNORECASE | re.DOTALL)


def _select_fields(record: Dict[str, Any], fields: Optional[str]) -> Dict[str, Any]:
    if not fields:
        return dict(record)
    wanted = {name.strip() for name in fields.split(",")}
    return {name: value for name, value in record.items() if name in wanted}


class FakeLooker:
    """In-memory state & router matching documented Looker API 4.0 payloads."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._users: Dict[str, Dict[str, Any]] = {}
        self._admins: Set[str] = set()
        self._groups: Dict[str, Dict[str, Any]] = {}
        self._user_attributes: Dict[str, str] = {}
        self._folder_groups: Dict[str, Set[str]] = {}
        self._api_keys: Dict[str, Dict[str, str]] = {}
        self._tokens: Dict[str, Dict[str, Any]] = {}
        self._effective_at: Dict[Tuple[str, str], float] = {}
        self._ids = itertools.count(1)
        self.attribute_values: Dict[Tuple[str, str], str] = {}
        self.hidden_attributes: Set[str] = set()
        self.conflict_on_duplicate_group = False
        self.calls: List[Tuple[str, str]] = []
        self.login_count = 0
        self.fail_next: List[int] = []
        self.retry_after_s: Optional[float] = None
        self.membership_delay_s = 0.0
        self.snapshot_groups_at_login = False

    def add_user(
        self,
        user_id: str,
        email: str,
        *,
        admin: bool = False,
        group_ids: Iterable[str] = (),
        disabled: bool = False,
        service_account: bool = False,
        saml_email: Optional[str] = None,
        saml_disabled: bool = False,
    ) -> None:
        with self._lock:
            record: Dict[str, Any] = {
                "id": user_id,
                "email": email,
                "is_disabled": disabled,
                "is_service_account": service_account,
                "group_ids": list(group_ids),
                "credentials_saml": None,
            }
            if saml_email is not None:
                record["credentials_saml"] = {
                    "email": saml_email,
                    "is_disabled": saml_disabled,
                    "saml_user_id": saml_email,
                }
            self._users[user_id] = record
            if admin:
                self._admins.add(user_id)

    def add_group(
        self, group_id: str, name: str, *, externally_managed: bool = False
    ) -> None:
        with self._lock:
            self._groups[group_id] = {
                "id": group_id,
                "name": name,
                "externally_managed": externally_managed,
            }

    def add_user_attribute(
        self, attribute_id: str, name: str, *, hidden: bool = False
    ) -> None:
        with self._lock:
            self._user_attributes[attribute_id] = name
            if hidden:
                self.hidden_attributes.add(attribute_id)

    def add_folder(self, folder_id: str, group_ids: Iterable[str]) -> None:
        with self._lock:
            self._folder_groups[folder_id] = set(group_ids)

    def add_api_key(self, user_id: str, client_id: str, client_secret: str) -> None:
        with self._lock:
            self._api_keys[client_id] = {
                "id": str(next(self._ids)),
                "user_id": user_id,
                "client_secret": client_secret,
            }

    def expire_tokens(self) -> None:
        with self._lock:
            self._tokens.clear()

    def group_ids_of(self, user_id: str) -> List[str]:
        with self._lock:
            return list(self._users[user_id]["group_ids"])

    def api_key_count(self, user_id: str) -> int:
        with self._lock:
            return sum(
                1 for key in self._api_keys.values() if key["user_id"] == user_id
            )

    def writes(self) -> List[Tuple[str, str]]:
        with self._lock:
            return [call for call in self.calls if call[0] != "GET"]

    def handle(
        self,
        method: str,
        path: str,
        query: Dict[str, str],
        authorization: Optional[str],
        body: bytes,
    ) -> Tuple[int, Any]:
        with self._lock:
            if (method, path) == ("POST", "/login"):
                return self._login(body)
            token = (authorization or "").partition("token ")[2].strip()
            session = self._tokens.get(token)
            if session is None:
                return 401, {"message": "Requires authentication."}
            self.calls.append((method, path))
            if self.fail_next:
                return self.fail_next.pop(0), {"message": "Injected failure"}
            return self._route(method, path, query, session, body)

    def _login(self, body: bytes) -> Tuple[int, Any]:
        form = urllib.parse.parse_qs(body.decode("utf-8"))
        client_id = (form.get("client_id") or [""])[0]
        key = self._api_keys.get(client_id)
        if key is None or key["client_secret"] != (
            form.get("client_secret") or [""]
        )[0]:
            return _NOT_FOUND
        token = f"token-{next(self._ids)}"
        user_id = key["user_id"]
        self._tokens[token] = {
            "user_id": user_id,
            "groups": frozenset(self._effective_groups(user_id)),
        }
        self.login_count += 1
        return 200, {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    def _effective_groups(self, user_id: str) -> Set[str]:
        now = self._clock()
        return {
            group_id
            for group_id in self._users[user_id]["group_ids"]
            if self._effective_at.get((group_id, user_id), 0.0) <= now
        }

    def _route(
        self,
        method: str,
        path: str,
        query: Dict[str, str],
        session: Dict[str, Any],
        body: bytes,
    ) -> Tuple[int, Any]:
        caller = session["user_id"]
        is_admin = caller in self._admins
        fields = query.get("fields")

        if (method, path) == ("GET", "/user"):
            return 200, _select_fields(self._users[caller], fields)

        match = re.fullmatch(r"/folders/([^/]+)", path)
        if match and method == "GET":
            folder_id = match.group(1)
            if folder_id not in self._folder_groups:
                return _NOT_FOUND
            groups = (
                session["groups"]
                if self.snapshot_groups_at_login
                else self._effective_groups(caller)
            )
            if not is_admin and not groups & self._folder_groups[folder_id]:
                return _NOT_FOUND
            return 200, _select_fields(
                {"id": folder_id, "name": f"Folder {folder_id}"}, fields
            )

        if not is_admin:
            return _NOT_FOUND

        if (method, path) == ("GET", "/users/search"):
            target_email = (query.get("email") or "").strip().lower()
            return 200, [
                _select_fields(user, fields)
                for user in self._users.values()
                if not target_email
                or (user.get("email") or "").strip().lower() == target_email
            ]

        match = re.fullmatch(r"/users/([^/]+)", path)
        if match and method == "GET":
            user = self._users.get(match.group(1))
            return (200, _select_fields(user, fields)) if user else _NOT_FOUND

        if (method, path) == ("GET", "/groups"):
            return 200, [
                _select_fields(group, fields) for group in self._groups.values()
            ]

        if (method, path) == ("GET", "/groups/search"):
            pattern = _like_to_regex(query.get("name", "%"))
            return 200, [
                _select_fields(group, fields)
                for group in self._groups.values()
                if pattern.fullmatch(group["name"])
            ]

        match = re.fullmatch(r"/groups/([^/]+)/users", path)
        if match and method == "POST":
            group_id = match.group(1)
            user_id = str(json.loads(body or b"{}").get("user_id", ""))
            if group_id not in self._groups or user_id not in self._users:
                return _NOT_FOUND
            members = self._users[user_id]["group_ids"]
            if group_id in members:
                if self.conflict_on_duplicate_group:
                    return 409, {"message": "User is already in group"}
            else:
                members.append(group_id)
                self._effective_at[(group_id, user_id)] = (
                    self._clock() + self.membership_delay_s
                )
            return 200, dict(self._users[user_id])

        match = re.fullmatch(r"/groups/([^/]+)/users/([^/]+)", path)
        if match and method == "DELETE":
            group_id, user_id = match.groups()
            if user_id not in self._users:
                return _NOT_FOUND
            members = self._users[user_id]["group_ids"]
            if group_id in members:
                members.remove(group_id)
            return 204, None

        if (method, path) == ("GET", "/user_attributes"):
            return 200, [
                _select_fields({"id": attribute_id, "name": name}, fields)
                for attribute_id, name in self._user_attributes.items()
            ]

        match = re.fullmatch(r"/users/([^/]+)/attribute_values", path)
        if match and method == "GET":
            user_id = match.group(1)
            if user_id not in self._users:
                return _NOT_FOUND
            raw_ids = query.get("user_attribute_ids")
            wanted = (
                {item.strip() for item in raw_ids.split(",") if item.strip()}
                if raw_ids
                else set(self._user_attributes)
            )
            rows = []
            for attribute_id, name in self._user_attributes.items():
                if attribute_id not in wanted:
                    continue
                if (user_id, attribute_id) not in self.attribute_values:
                    continue
                is_hidden = attribute_id in self.hidden_attributes
                rows.append(
                    _select_fields(
                        {
                            "user_id": user_id,
                            "user_attribute_id": attribute_id,
                            "name": name,
                            "value": (
                                None
                                if is_hidden
                                else self.attribute_values[
                                    (user_id, attribute_id)
                                ]
                            ),
                            "value_is_hidden": is_hidden,
                            "source": "User",
                        },
                        fields,
                    )
                )
            return 200, rows

        match = re.fullmatch(r"/users/([^/]+)/attribute_values/([^/]+)", path)
        if match and method == "PATCH":
            user_id, attribute_id = match.groups()
            if (
                user_id not in self._users
                or attribute_id not in self._user_attributes
            ):
                return _NOT_FOUND
            value = json.loads(body or b"{}").get("value")
            self.attribute_values[(user_id, attribute_id)] = value
            return 200, {
                "user_id": user_id,
                "user_attribute_id": attribute_id,
                "name": self._user_attributes[attribute_id],
                "value": value,
            }

        match = re.fullmatch(r"/users/([^/]+)/credentials_api3", path)
        if match and method == "POST":
            user_id = match.group(1)
            if user_id not in self._users:
                return _NOT_FOUND
            client_id = f"generated-{next(self._ids)}"
            secret = f"secret-{next(self._ids)}"
            self.add_api_key(user_id, client_id, secret)
            return 200, {
                "id": self._api_keys[client_id]["id"],
                "client_id": client_id,
                "client_secret": secret,
            }

        match = re.fullmatch(r"/users/([^/]+)/credentials_api3/([^/]+)", path)
        if match and method == "DELETE":
            user_id, credentials_id = match.groups()
            for client_id, key in list(self._api_keys.items()):
                if key["user_id"] == user_id and key["id"] == credentials_id:
                    del self._api_keys[client_id]
                    return 204, None
            return _NOT_FOUND

        return _NOT_FOUND


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _dispatch(self) -> None:
        split = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if split.path.startswith(API_PREFIX):
            query = {
                name: values[0]
                for name, values in urllib.parse.parse_qs(split.query).items()
            }
            status, payload = self.server.fake.handle(  # type: ignore[attr-defined]
                self.command,
                split.path[len(API_PREFIX):],
                query,
                self.headers.get("Authorization"),
                body,
            )
        else:
            status, payload = _NOT_FOUND
        data = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        retry_after_s = self.server.fake.retry_after_s  # type: ignore[attr-defined]
        if status in (429, 503) and retry_after_s is not None:
            self.send_header("Retry-After", str(retry_after_s))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _dispatch  # pylint: disable=invalid-name

    def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
        pass


class FakeLookerServer:
    """Serves a FakeLooker over HTTP on a loopback port."""

    def __init__(self, fake: FakeLooker, port: int = 0):
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.fake = fake  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()

    def __enter__(self) -> "FakeLookerServer":
        return self

    def __exit__(self, *unused_exc_info: Any) -> None:
        self.close()


def seed_demo(fake: FakeLooker) -> None:
    """Loads demo users, groups, attributes, and folder for offline testing."""
    fake.add_user("1", "admin@example.com", admin=True, service_account=True)
    fake.add_api_key("1", "demo-admin-id", "demo-admin-secret")
    fake.add_user(
        "101",
        "alice@example.com",
        group_ids=["1", "10"],
        saml_email="alice@example.com",
    )
    fake.add_user(
        "102",
        "bob@example.com",
        group_ids=["1", "10"],
        saml_email="bob@example.com",
    )
    fake.add_user(
        "103",
        "carol@example.com",
        group_ids=["1", "10"],
        saml_email="carol@example.com",
    )
    fake.add_group("1", "All Users")
    fake.add_group("10", "SAML_BI_Users", externally_managed=True)
    fake.add_group("20", "BI_Finance_EMEA")
    fake.add_group("21", "BI_Dashboard_Hub")
    fake.add_group("22", "BI_Marketing_Global")
    fake.add_group("99", "BI_Access_Pending_Approval")
    fake.add_user_attribute("1", "landing_page")
    fake.add_user_attribute("2", "region")
    fake.add_folder("67", ["20"])


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args(argv)
    fake = FakeLooker()
    seed_demo(fake)
    server = FakeLookerServer(fake, port=args.port)
    print(f"Fake Looker listening on {server.base_url}")
    print("Admin API key: demo-admin-id / demo-admin-secret")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

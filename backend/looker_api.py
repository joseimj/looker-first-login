"""Minimal Looker API 4.0 client built on the Python standard library.

Wraps the endpoints needed by both SMB and Enterprise provisioning profiles.
Logs in lazily, caches the access token for ~50 minutes (or uses an injected
shared token store across Cloud Run replicas), never calls POST /logout,
bounds in-flight concurrency, retries 429/5xx respecting Retry-After, and
counts Admin API calls for quota tracking.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

API_PREFIX = "/api/4.0"

_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
# Looker tokens last 60 minutes; refresh 10 minutes early (50m effective TTL).
_TOKEN_EXPIRY_MARGIN_S = 600.0
_MAX_RETRY_DELAY_S = 30.0
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class LookerApiError(Exception):
    """A Looker API call failed or returned an unexpected HTTP status."""

    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:300]}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


def _quote(value: Any) -> str:
    return urllib.parse.quote(str(value), safe="")


class LookerApi:
    """Thread-safe Looker API 4.0 client for one set of API3 credentials."""

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        *,
        timeout_s: float = 20.0,
        max_retries: int = 3,
        max_concurrency: int = 10,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_concurrency < 1:
            raise ValueError(
                f"max_concurrency must be at least 1, got {max_concurrency!r}"
            )
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme != "https" and not (
            parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS
        ):
            raise ValueError(
                "base_url must use https (http is allowed for localhost only): "
                f"{base_url!r}"
            )
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self._call_count = 0
        self._login_count = 0

    @property
    def call_count(self) -> int:
        """Number of API calls sent so far, retries included, logins excluded."""
        with self._lock:
            return self._call_count

    @property
    def login_count(self) -> int:
        """Number of successful logins so far."""
        with self._lock:
            return self._login_count

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        body: Any = None,
    ) -> Any:
        """Calls Looker API 4.0 and returns the decoded JSON response."""
        url = self._base_url + API_PREFIX + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif method in ("POST", "PUT", "PATCH"):
            data = b""

        reauthenticated = False
        attempt = 0
        while True:
            token = self._access_token()
            headers["Authorization"] = "token " + token
            with self._lock:
                self._call_count += 1
            status, response_headers, text = self._http(
                method, path, url, headers, data
            )
            if status == 401 and not reauthenticated:
                reauthenticated = True
                self._invalidate_token(token)
                continue
            if status in _RETRYABLE_STATUSES and attempt < self._max_retries:
                self._sleep(_retry_delay_s(attempt, response_headers))
                attempt += 1
                continue
            if 200 <= status < 300:
                return json.loads(text) if text.strip() else None
            raise LookerApiError(method, path, status, text)

    # Endpoint wrappers.

    def me(self, fields: str = "id,email,display_name") -> Any:
        return self.request("GET", "/user", query={"fields": fields})

    def user(self, user_id: Any, fields: str) -> Any:
        return self.request(
            "GET", f"/users/{_quote(user_id)}", query={"fields": fields}
        )

    def search_users_by_email(
        self, email: str, fields: str = "id,email,is_disabled,group_ids"
    ) -> List[Any]:
        return self.request(
            "GET", "/users/search", query={"email": email, "fields": fields}
        )

    def all_groups(self, fields: str = "id,name,externally_managed") -> List[Any]:
        return self.request("GET", "/groups", query={"fields": fields})

    def search_groups_by_name(self, name: str) -> List[Any]:
        """Returns groups whose name matches (case-insensitive SQL LIKE)."""
        return self.request(
            "GET",
            "/groups/search",
            query={"name": name, "fields": "id,name,externally_managed"},
        )

    def add_group_user(self, group_id: Any, user_id: Any) -> Any:
        return self.request(
            "POST",
            f"/groups/{_quote(group_id)}/users",
            body={"user_id": str(user_id)},
        )

    def delete_group_user(self, group_id: Any, user_id: Any) -> None:
        self.request(
            "DELETE", f"/groups/{_quote(group_id)}/users/{_quote(user_id)}"
        )

    def all_user_attributes(self) -> List[Any]:
        return self.request(
            "GET", "/user_attributes", query={"fields": "id,name"}
        )

    def user_attribute_user_values(
        self,
        user_id: Any,
        user_attribute_ids: Optional[Sequence[Any]] = None,
    ) -> List[Any]:
        """Returns effective user attribute values for one user."""
        query: Dict[str, Any] = {
            "fields": "user_attribute_id,name,value,source,value_is_hidden"
        }
        if user_attribute_ids:
            query["user_attribute_ids"] = ",".join(
                str(attr_id) for attr_id in user_attribute_ids
            )
        return self.request(
            "GET",
            f"/users/{_quote(user_id)}/attribute_values",
            query=query,
        )

    def set_user_attribute_user_value(
        self, user_id: Any, user_attribute_id: Any, value: str
    ) -> Any:
        return self.request(
            "PATCH",
            f"/users/{_quote(user_id)}/attribute_values/"
            f"{_quote(user_attribute_id)}",
            body={"value": value},
        )

    def create_user_credentials_api3(self, user_id: Any) -> Any:
        return self.request(
            "POST", f"/users/{_quote(user_id)}/credentials_api3"
        )

    def delete_user_credentials_api3(
        self, user_id: Any, credentials_id: Any
    ) -> None:
        self.request(
            "DELETE",
            f"/users/{_quote(user_id)}/credentials_api3/"
            f"{_quote(credentials_id)}",
        )

    # Internals.

    def _access_token(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._token_expiry:
                self._login_locked()
            assert self._token is not None
            return self._token

    def _invalidate_token(self, token: str) -> None:
        with self._lock:
            if self._token == token:
                self._token = None

    def _login_locked(self) -> None:
        data = urllib.parse.urlencode(
            {"client_id": self._client_id, "client_secret": self._client_secret}
        ).encode("utf-8")
        status, _, text = self._http(
            "POST",
            "/login",
            self._base_url + API_PREFIX + "/login",
            {"Content-Type": "application/x-www-form-urlencoded"},
            data,
        )
        if status != 200:
            raise LookerApiError("POST", "/login", status, text)
        payload = json.loads(text)
        self._token = payload["access_token"]
        lifetime_s = float(payload.get("expires_in") or 3600)
        self._token_expiry = (
            self._clock() + max(lifetime_s - _TOKEN_EXPIRY_MARGIN_S, 0.0)
        )
        self._login_count += 1

    def _http(
        self,
        method: str,
        path: str,
        url: str,
        headers: Mapping[str, str],
        data: Optional[bytes],
    ) -> Tuple[int, Mapping[str, str], str]:
        request = urllib.request.Request(
            url, data=data, headers=dict(headers), method=method
        )
        with self._semaphore:
            try:
                with urllib.request.urlopen(
                    request, timeout=self._timeout_s
                ) as response:
                    body = response.read().decode("utf-8", "replace")
                    return response.status, response.headers, body
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", "replace")
                return error.code, error.headers, body
            except (urllib.error.URLError, OSError) as error:
                raise LookerApiError(method, path, 0, str(error)) from error


def _retry_delay_s(attempt: int, headers: Mapping[str, str]) -> float:
    """Returns wait before a retry, honoring Retry-After when present."""
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), _MAX_RETRY_DELAY_S)
        except ValueError:
            pass
    return min(0.5 * (2**attempt), _MAX_RETRY_DELAY_S)

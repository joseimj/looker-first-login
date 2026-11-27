"""Pluggable entitlements sources for SMB (JSON file) and Enterprise (IGA API).

Every source returns an Entitlement object (or None when the user has no
entitlements). Enterprise sources can also expose a change feed so delta
revocation only touches users whose access changed, instead of scanning all
10,000 Looker users.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Tuple


class EntitlementsError(Exception):
    """Raised when an external entitlements source cannot be queried."""


@dataclasses.dataclass(frozen=True)
class Entitlement:
    """What one Looker user is entitled to."""

    groups: Tuple[str, ...] = ()
    user_attributes: Mapping[str, str] = dataclasses.field(default_factory=dict)
    landing_page: Optional[str] = None

    def digest(self) -> str:
        """Stable SHA-256 hash stored in the zero-PII ledger for drift checks."""
        canonical = json.dumps(
            {
                "groups": sorted(self.groups),
                "user_attributes": sorted(self.user_attributes.items()),
                "landing_page": self.landing_page,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_relative_looker_url(value: Any) -> bool:
    """Returns whether value is a safe relative path on the Looker instance."""
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith(("//", "/\\"))
        and not any(char.isspace() for char in value)
    )


def parse_entitlement(raw: Any) -> Entitlement:
    """Validates and parses one entitlement entry."""
    if not isinstance(raw, dict):
        raise ValueError(f"entitlement must be an object, got {raw!r}")
    groups = raw.get("groups", [])
    if not isinstance(groups, list) or not all(
        isinstance(name, str) and name for name in groups
    ):
        raise ValueError(f"'groups' must be a list of names, got {groups!r}")
    attributes = raw.get("user_attributes", {})
    if not isinstance(attributes, dict) or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in attributes.items()
    ):
        raise ValueError(
            f"'user_attributes' must map names to strings, got {attributes!r}"
        )
    landing_page = raw.get("landing_page")
    if landing_page is not None and not is_relative_looker_url(landing_page):
        raise ValueError(
            f"'landing_page' must be a relative URL such as /dashboards/1, "
            f"got {landing_page!r}"
        )
    return Entitlement(
        groups=tuple(dict.fromkeys(groups)),
        user_attributes=dict(attributes),
        landing_page=landing_page,
    )


class EntitlementsFile:
    """SMB / PoC source: reads entitlements from a local JSON file.

    Automatically reloads when the file's mtime changes. Expected format:

        {
          "users": {
            "alice@example.com": {
              "groups": ["BI_Finance_EMEA", "BI_Dashboard_Hub"],
              "user_attributes": {"region": "EMEA"},
              "landing_page": "/dashboards/12"
            }
          },
          "default": null,
          "changed_emails": []
        }
    """

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._mtime_ns: Optional[int] = None
        self._users: Dict[str, Entitlement] = {}
        self._default: Optional[Entitlement] = None
        self._changed_emails: Tuple[str, ...] = ()
        self._reload_if_changed()

    def lookup(self, email: str) -> Optional[Entitlement]:
        self._reload_if_changed()
        with self._lock:
            return self._users.get(email.strip().lower(), self._default)

    def changed_emails(self, since: Optional[str] = None) -> Tuple[List[str], Optional[str]]:
        """Returns changed emails configured in the file (for local testing)."""
        del since  # Static JSON file has no server-side cursor.
        self._reload_if_changed()
        with self._lock:
            return list(self._changed_emails), None

    def _reload_if_changed(self) -> None:
        mtime_ns = os.stat(self._path).st_mtime_ns
        with self._lock:
            if mtime_ns == self._mtime_ns:
                return
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("entitlements file must contain a JSON object")
            users = data.get("users", {})
            if not isinstance(users, dict):
                raise ValueError("'users' must be an object keyed by email")
            self._users = {
                email.strip().lower(): parse_entitlement(entry)
                for email, entry in users.items()
            }
            default = data.get("default")
            self._default = (
                None if default is None else parse_entitlement(default)
            )
            changed = data.get("changed_emails", [])
            if not isinstance(changed, list):
                raise ValueError("'changed_emails' must be a list of emails")
            self._changed_emails = tuple(
                str(e).strip().lower() for e in changed if str(e).strip()
            )
            self._mtime_ns = mtime_ns


class HttpIgaEntitlements:
    """Enterprise source: queries an internal IGA / HR REST API over HTTPS.

    Contract (adaptable to SailPoint, Saviynt, Workday, or a custom facade):
      - GET {base_url}/entitlements?email=<email>
        -> 200 {"groups": [...], "user_attributes": {...}, "landing_page": "..."}
        -> 404 (no entitlements)
      - GET {base_url}/changes?since=<cursor>
        -> 200 {"emails": ["a@corp.com", ...], "next_cursor": "..."}
    """

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: Optional[str] = None,
        api_key_header: Optional[Tuple[str, str]] = None,
        timeout_s: float = 10.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._api_key_header = api_key_header
        self._timeout_s = timeout_s

    def lookup(self, email: str) -> Optional[Entitlement]:
        query = urllib.parse.urlencode({"email": email.strip().lower()})
        status, body = self._get(f"{self._base_url}/entitlements?{query}")
        if status == 404:
            return None
        if status != 200:
            raise EntitlementsError(
                f"IGA entitlements lookup returned HTTP {status}: {body[:200]}"
            )
        try:
            payload = json.loads(body)
        except ValueError as error:
            raise EntitlementsError(f"Invalid IGA JSON: {error}") from error
        if not payload:
            return None
        return parse_entitlement(payload)

    def changed_emails(
        self, since: Optional[str] = None
    ) -> Tuple[List[str], Optional[str]]:
        url = f"{self._base_url}/changes"
        if since:
            url += "?" + urllib.parse.urlencode({"since": since})
        status, body = self._get(url)
        if status != 200:
            raise EntitlementsError(
                f"IGA change feed returned HTTP {status}: {body[:200]}"
            )
        try:
            payload = json.loads(body)
        except ValueError as error:
            raise EntitlementsError(f"Invalid IGA changes JSON: {error}") from error
        emails = [
            str(item).strip().lower()
            for item in (payload.get("emails") or [])
            if str(item).strip()
        ]
        next_cursor = payload.get("next_cursor")
        return emails, (str(next_cursor) if next_cursor is not None else None)

    def _get(self, url: str) -> Tuple[int, str]:
        headers = {"Accept": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        if self._api_key_header:
            headers[self._api_key_header[0]] = self._api_key_header[1]
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_s
            ) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as error:
            raise EntitlementsError(f"IGA request failed: {error}") from error

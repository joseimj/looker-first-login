"""Idempotent first-login provisioning of a Looker user.

Given a Looker user ID, the provisioner reads the user's email from Looker
with admin credentials (the caller-supplied identity is never trusted beyond
the ID), looks the email up in the entitlements source, and applies:

1. data-restricting user attributes (row-level security) first,
2. group memberships second,
3. the personal landing page last.

That order fails closed: a user never holds a group before the attributes
that filter their data are set, and a user whose provisioning was interrupted
keeps landing on the extension, which repairs only what is still missing.
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import looker_api

STATUS_PROVISIONED = "provisioned"
STATUS_ALREADY_PROVISIONED = "already_provisioned"
STATUS_NO_ENTITLEMENTS = "no_entitlements"
STATUS_UNKNOWN_USER = "unknown_user"
STATUS_DISABLED = "disabled"
STATUS_NOT_SAML = "not_saml"


class ConfigError(Exception):
    """The entitlements refer to a group or attribute Looker doesn't have."""


@dataclasses.dataclass(frozen=True)
class Entitlement:
    """What one user is entitled to.

    Attributes:
        groups: Names of the Looker groups the user must belong to.
        user_attributes: Looker user attribute values, keyed by attribute name.
        landing_page: Relative Looker URL the user lands on once provisioned.
    """

    groups: Tuple[str, ...] = ()
    user_attributes: Mapping[str, str] = dataclasses.field(default_factory=dict)
    landing_page: Optional[str] = None


def is_relative_looker_url(value: Any) -> bool:
    """Returns whether value is a path on the Looker instance itself."""
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith(("//", "/\\"))
        and not any(char.isspace() for char in value)
    )


def parse_entitlement(raw: Any) -> Entitlement:
    """Validates one entitlement entry of the JSON file.

    Args:
        raw: Decoded JSON object with optional "groups", "user_attributes" and
            "landing_page" members.

    Returns:
        The parsed entitlement.

    Raises:
        ValueError: If the entry is malformed.
    """
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
    """Entitlements read from a JSON file; stands in for the real database.

    The file is re-read whenever it changes on disk, which is convenient while
    testing. Expected shape:

        {"users": {"<email>": {"groups": [...], "user_attributes": {...},
                               "landing_page": "/dashboards/1"}},
         "default": null}

    "default" is optional; when set it applies to users without an entry.
    """

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._mtime_ns: Optional[int] = None
        self._users: Dict[str, Entitlement] = {}
        self._default: Optional[Entitlement] = None
        self._reload_if_changed()

    def lookup(self, email: str) -> Optional[Entitlement]:
        """Returns the entitlement for an email address, if any."""
        self._reload_if_changed()
        with self._lock:
            return self._users.get(email.strip().lower(), self._default)

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
            self._mtime_ns = mtime_ns


class Provisioner:
    """Applies entitlements to Looker users through the Looker API."""

    def __init__(
        self,
        api: looker_api.LookerApi,
        entitlements: Any,
        *,
        set_landing_page: bool = True,
        landing_page_attribute: str = "landing_page",
        require_saml: bool = False,
        dry_run: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ):
        """Initializes the provisioner.

        Args:
            api: Looker API client authenticated as an admin.
            entitlements: Object with a lookup(email) -> Optional[Entitlement]
                method.
            set_landing_page: Whether to store the entitlement's landing page
                as the user's own landing_page value. When False the page is
                only returned for the redirect, saving one admin API call.
            landing_page_attribute: Name of the Looker landing page attribute.
            require_saml: When True, only accounts with active SAML credentials
                are provisioned; other accounts return STATUS_NOT_SAML.
            dry_run: When True, nothing is written to Looker.
            clock: Monotonic clock, injectable for tests.
        """
        self._api = api
        self._entitlements = entitlements
        self._set_landing_page = set_landing_page
        self._landing_page_attribute = landing_page_attribute
        self._require_saml = require_saml
        self._dry_run = dry_run
        self._clock = clock
        self._cache_lock = threading.Lock()
        self._group_ids: Dict[str, str] = {}
        self._attribute_ids: Optional[Dict[str, str]] = None
        self._user_locks: Dict[str, threading.Lock] = {}

    def provision(self, user_id: str) -> Dict[str, Any]:
        """Brings one user in line with their entitlements.

        Safe to call repeatedly and concurrently: calls for the same user are
        serialized, and a user who already holds every entitled group and
        attribute triggers no writes.

        Args:
            user_id: Looker user ID.

        Returns:
            A dict with "status" (one of the STATUS_* constants), "redirect",
            "groups_added", "attributes_set", "landing_page_set", "dry_run",
            "admin_api_calls" (approximate when calls run concurrently) and
            "elapsed_ms".

        Raises:
            ConfigError: If an entitled group or attribute is missing in
                Looker. Nothing is written in that case.
            looker_api.LookerApiError: If a Looker API call fails.
        """
        with self._user_lock(user_id):
            return self._provision_locked(user_id)

    def _provision_locked(self, user_id: str) -> Dict[str, Any]:
        started = self._clock()
        calls_before = self._api.call_count
        result: Dict[str, Any] = {
            "status": STATUS_NO_ENTITLEMENTS,
            "redirect": None,
            "groups_added": [],
            "attributes_set": [],
            "landing_page_set": False,
            "dry_run": self._dry_run,
        }

        def done(status: str) -> Dict[str, Any]:
            result["status"] = status
            result["admin_api_calls"] = self._api.call_count - calls_before
            result["elapsed_ms"] = round((self._clock() - started) * 1000)
            return result

        try:
            user = self._api.user(
                user_id,
                fields=(
                    "id,email,is_disabled,is_service_account,"
                    "group_ids,credentials_saml"
                ),
            )
        except looker_api.LookerApiError as error:
            if error.status == 404:
                return done(STATUS_UNKNOWN_USER)
            raise
        if user.get("is_disabled") or user.get("is_service_account"):
            return done(STATUS_DISABLED)
        saml = user.get("credentials_saml") or {}
        if self._require_saml and (not saml or saml.get("is_disabled")):
            return done(STATUS_NOT_SAML)
        email = (user.get("email") or saml.get("email") or "").strip().lower()
        entitlement = self._entitlements.lookup(email) if email else None
        if entitlement is None:
            return done(STATUS_NO_ENTITLEMENTS)
        result["redirect"] = entitlement.landing_page

        # Resolve every name before the first write, so that a configuration
        # error can't leave the user half provisioned.
        target_groups = [
            (name, self._group_id(name)) for name in entitlement.groups
        ]
        attributes = [
            (name, self._attribute_id(name), value)
            for name, value in entitlement.user_attributes.items()
        ]
        landing_page_attribute_id = None
        if self._set_landing_page and entitlement.landing_page:
            landing_page_attribute_id = self._attribute_id(
                self._landing_page_attribute
            )

        current_groups = {
            str(group_id) for group_id in user.get("group_ids") or []
        }
        missing_groups = [
            (name, group_id)
            for name, group_id in target_groups
            if group_id not in current_groups
        ]

        if missing_groups:
            # First-login happy path: no extra read needed; write attributes
            # before adding groups so row-level security is active immediately.
            attrs_to_set = attributes
            need_landing_page = landing_page_attribute_id is not None
        else:
            # User already holds every entitled group. Check attributes and
            # landing_page once so interrupted runs or manual group additions
            # self-heal without redundant writes.
            wanted_ids = [aid for _, aid, _ in attributes]
            if landing_page_attribute_id is not None:
                wanted_ids.append(landing_page_attribute_id)
            if wanted_ids:
                existing_values = self._current_attribute_values(
                    user_id, wanted_ids
                )
                attrs_to_set = [
                    (name, aid, value)
                    for name, aid, value in attributes
                    if existing_values.get(aid) != value
                ]
                need_landing_page = (
                    landing_page_attribute_id is not None
                    and existing_values.get(landing_page_attribute_id)
                    != entitlement.landing_page
                )
            else:
                attrs_to_set = []
                need_landing_page = False
            if not attrs_to_set and not need_landing_page:
                return done(STATUS_ALREADY_PROVISIONED)

        for name, attribute_id, value in attrs_to_set:
            if not self._dry_run:
                self._api.set_user_attribute_user_value(
                    user_id, attribute_id, value
                )
            result["attributes_set"].append(name)
        for name, group_id in missing_groups:
            if not self._dry_run:
                self._add_group_user_idempotent(group_id, user_id)
            result["groups_added"].append(name)
        if need_landing_page and landing_page_attribute_id is not None:
            assert entitlement.landing_page is not None
            if not self._dry_run:
                self._api.set_user_attribute_user_value(
                    user_id, landing_page_attribute_id, entitlement.landing_page
                )
            result["landing_page_set"] = True
        return done(STATUS_PROVISIONED)

    def _add_group_user_idempotent(self, group_id: str, user_id: str) -> None:
        """Adds the user to the group, tolerating a cross-replica 409 race."""
        try:
            self._api.add_group_user(group_id, user_id)
        except looker_api.LookerApiError as error:
            if error.status != 409:
                raise
            latest = self._api.user(user_id, fields="group_ids")
            if group_id not in {
                str(gid) for gid in latest.get("group_ids") or []
            }:
                raise

    def _current_attribute_values(
        self, user_id: str, attribute_ids: Sequence[str]
    ) -> Dict[str, Optional[str]]:
        """Reads current effective attribute values; hidden values stay None."""
        rows = self._api.user_attribute_user_values(user_id, attribute_ids)
        values: Dict[str, Optional[str]] = {}
        for row in rows or []:
            aid = str(row.get("user_attribute_id", ""))
            if aid:
                values[aid] = None if row.get("value_is_hidden") else row.get(
                    "value"
                )
        return values

    def _user_lock(self, user_id: str) -> threading.Lock:
        with self._cache_lock:
            return self._user_locks.setdefault(user_id, threading.Lock())

    def _group_id(self, name: str) -> str:
        with self._cache_lock:
            cached = self._group_ids.get(name)
        if cached is not None:
            return cached
        # The search is a case-insensitive SQL LIKE ("_" and "%" are
        # wildcards), so keep only the exact name.
        matches: List[Any] = [
            group
            for group in self._api.search_groups_by_name(name) or []
            if group.get("name") == name
        ]
        if not matches:
            raise ConfigError(f"Looker group not found: {name!r}")
        if len(matches) > 1:
            raise ConfigError(f"Several Looker groups are named {name!r}")
        group_id = str(matches[0]["id"])
        with self._cache_lock:
            self._group_ids[name] = group_id
        return group_id

    def _attribute_id(self, name: str) -> str:
        with self._cache_lock:
            attribute_ids = self._attribute_ids
        if attribute_ids is None:
            attribute_ids = {
                attribute["name"]: str(attribute["id"])
                for attribute in self._api.all_user_attributes() or []
            }
            with self._cache_lock:
                self._attribute_ids = attribute_ids
        if name not in attribute_ids:
            raise ConfigError(f"Looker user attribute not found: {name!r}")
        return attribute_ids[name]

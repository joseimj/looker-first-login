"""Unified fail-closed delta provisioner for SMB and Enterprise profiles.

Given a Looker user ID, the provisioner:
1. Reads the authoritative user record from Looker using Admin credentials
   (never trusting client-supplied email/groups).
2. Skips disabled accounts, API-only service accounts, and (when configured)
   non-SAML accounts.
3. Looks up entitlements from the configured EntitlementsSource (JSON file for
   SMB, internal IGA/HR API for Enterprise).
4. If the user has zero entitlements and `pending_group_name` is configured
   (Enterprise Passive Polling Extension pattern), assigns that zero-privilege
   marker group so the splash screen stops polling immediately and displays an
   actionable self-service access link.
5. Otherwise applies writes in strict fail-closed order:
     a) Data-restricting user attributes (RLS) first (0 calls when using
        Warehouse-Native BigQuery RLS).
     b) Missing group memberships second (tolerating cross-replica 409 races).
     c) Optional safe delta revocation ONLY within `managed_group_prefix` or
        `managed_groups` (never removing All Users, SAML mirrored, or Admin
        groups) plus removing the pending marker group if previously assigned.
     d) Personal `landing_page` last (skipped when `--no-landing-page` /
        group-ranked `landing_page` is used).
6. Records the zero-PII outcome + entitlement hash in the ledger if attached.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

import entitlements as entitlements_lib
import looker_api

STATUS_PROVISIONED = "provisioned"
STATUS_ALREADY_PROVISIONED = "already_provisioned"
STATUS_NO_ENTITLEMENTS = "no_entitlements"
STATUS_UNKNOWN_USER = "unknown_user"
STATUS_DISABLED = "disabled"
STATUS_NOT_SAML = "not_saml"


class ConfigError(Exception):
    """The entitlements refer to a Looker group or attribute that doesn't exist."""


class Provisioner:
    """Applies fail-closed, idempotent delta provisioning via Looker API 4.0."""

    def __init__(
        self,
        api: looker_api.LookerApi,
        entitlements: Any,
        *,
        set_landing_page: bool = True,
        landing_page_attribute: str = "landing_page",
        require_saml: bool = False,
        pending_group_name: Optional[str] = None,
        managed_group_prefix: Optional[str] = None,
        managed_groups: FrozenSet[str] = frozenset(),
        ledger: Any = None,
        dry_run: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._api = api
        self._entitlements = entitlements
        self._set_landing_page = set_landing_page
        self._landing_page_attribute = landing_page_attribute
        self._require_saml = require_saml
        self._pending_group_name = pending_group_name
        self._managed_group_prefix = managed_group_prefix
        self._managed_groups = managed_groups
        self._ledger = ledger
        self._dry_run = dry_run
        self._clock = clock
        self._cache_lock = threading.Lock()
        self._group_ids: Dict[str, str] = {}
        self._managed_catalog: Optional[Dict[str, str]] = None
        self._attribute_ids: Optional[Dict[str, str]] = None
        self._user_locks: Dict[str, threading.Lock] = {}

    def provision(
        self, user_id: str, *, revoke: bool = False
    ) -> Dict[str, Any]:
        """Provisions (and optionally revokes stale managed groups for) a user."""
        with self._user_lock(user_id):
            return self._provision_locked(user_id, revoke=revoke)

    def _provision_locked(
        self, user_id: str, *, revoke: bool
    ) -> Dict[str, Any]:
        started = self._clock()
        calls_before = self._api.call_count
        result: Dict[str, Any] = {
            "status": STATUS_NO_ENTITLEMENTS,
            "redirect": None,
            "groups_added": [],
            "groups_removed": [],
            "attributes_set": [],
            "landing_page_set": False,
            "pending_marker_set": False,
            "dry_run": self._dry_run,
        }

        def done(status: str, ent_hash: str = "") -> Dict[str, Any]:
            result["status"] = status
            result["admin_api_calls"] = self._api.call_count - calls_before
            result["elapsed_ms"] = round((self._clock() - started) * 1000)
            if self._ledger is not None and not self._dry_run:
                self._ledger.record_result(user_id, status, ent_hash)
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
        entitlement: Optional[entitlements_lib.Entitlement] = (
            self._entitlements.lookup(email) if email else None
        )
        current_groups = {
            str(group_id) for group_id in user.get("group_ids") or []
        }

        if entitlement is None:
            # Optional safe revocation when a previously entitled user loses
            # all entitlements in Enterprise IGA.
            if revoke:
                for name, group_id in self._stale_managed_groups(
                    current_groups, keep_ids=frozenset()
                ):
                    if not self._dry_run:
                        self._api.delete_group_user(group_id, user_id)
                    result["groups_removed"].append(name)
            # Assign zero-privilege marker group so Passive Polling Extension
            # stops immediately instead of waiting until timeout.
            if self._pending_group_name:
                pending_id = self._group_id(self._pending_group_name)
                if pending_id not in current_groups:
                    if not self._dry_run:
                        self._add_group_user_idempotent(pending_id, user_id)
                    result["pending_marker_set"] = True
            return done(STATUS_NO_ENTITLEMENTS)

        ent_hash = entitlement.digest()
        result["redirect"] = entitlement.landing_page

        # Resolve every Looker ID before any write so misconfiguration never
        # leaves a user partially provisioned.
        target_groups = [
            (name, self._group_id(name)) for name in entitlement.groups
        ]
        target_ids = frozenset(gid for _, gid in target_groups)
        pending_id = (
            self._group_id(self._pending_group_name)
            if self._pending_group_name
            else None
        )
        attributes = [
            (name, self._attribute_id(name), value)
            for name, value in entitlement.user_attributes.items()
        ]
        landing_page_attribute_id = None
        if self._set_landing_page and entitlement.landing_page:
            landing_page_attribute_id = self._attribute_id(
                self._landing_page_attribute
            )

        missing_groups = [
            (name, gid)
            for name, gid in target_groups
            if gid not in current_groups
        ]
        stale_groups = (
            self._stale_managed_groups(current_groups, keep_ids=target_ids)
            if revoke
            else []
        )
        remove_pending = (
            pending_id is not None
            and pending_id in current_groups
            and pending_id not in target_ids
        )

        if missing_groups:
            attrs_to_set = attributes
            need_landing_page = landing_page_attribute_id is not None
        else:
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
            if (
                not attrs_to_set
                and not need_landing_page
                and not stale_groups
                and not remove_pending
            ):
                return done(STATUS_ALREADY_PROVISIONED, ent_hash)

        # Fail-closed write sequence:
        # 1. RLS user attributes first (0 writes when using BigQuery RLS).
        for name, attribute_id, value in attrs_to_set:
            if not self._dry_run:
                self._api.set_user_attribute_user_value(
                    user_id, attribute_id, value
                )
            result["attributes_set"].append(name)

        # 2. Add missing target groups.
        for name, group_id in missing_groups:
            if not self._dry_run:
                self._add_group_user_idempotent(group_id, user_id)
            result["groups_added"].append(name)

        # 3. Remove stale managed groups (and clear pending marker if set).
        for name, group_id in stale_groups:
            if not self._dry_run:
                self._api.delete_group_user(group_id, user_id)
            result["groups_removed"].append(name)
        if remove_pending and pending_id is not None:
            assert self._pending_group_name is not None
            if not self._dry_run:
                self._api.delete_group_user(pending_id, user_id)
            result["groups_removed"].append(self._pending_group_name)

        # 4. Update per-user landing_page last (skipped if --no-landing-page).
        if need_landing_page and landing_page_attribute_id is not None:
            assert entitlement.landing_page is not None
            if not self._dry_run:
                self._api.set_user_attribute_user_value(
                    user_id, landing_page_attribute_id, entitlement.landing_page
                )
            result["landing_page_set"] = True

        return done(STATUS_PROVISIONED, ent_hash)

    def _stale_managed_groups(
        self, current_ids: FrozenSet[str] | set[str], keep_ids: FrozenSet[str]
    ) -> List[Tuple[str, str]]:
        """Returns (name, id) pairs safe to revoke under the managed scope.

        Never touches All Users, externally managed (SAML/OIDC) groups, or any
        group outside managed_group_prefix / managed_groups.
        """
        if not self._managed_group_prefix and not self._managed_groups:
            return []
        catalog = self._load_managed_catalog()
        return [
            (name, gid)
            for gid, name in catalog.items()
            if gid in current_ids and gid not in keep_ids
        ]

    def _load_managed_catalog(self) -> Dict[str, str]:
        with self._cache_lock:
            if self._managed_catalog is not None:
                return self._managed_catalog
        catalog: Dict[str, str] = {}
        for group in self._api.all_groups() or []:
            if group.get("externally_managed"):
                continue
            name = str(group.get("name") or "")
            gid = str(group.get("id") or "")
            if not name or not gid or name == "All Users":
                continue
            if name == self._pending_group_name:
                continue
            in_prefix = bool(
                self._managed_group_prefix
                and name.startswith(self._managed_group_prefix)
            )
            if in_prefix or name in self._managed_groups:
                catalog[gid] = name
                with self._cache_lock:
                    self._group_ids[name] = gid
        with self._cache_lock:
            self._managed_catalog = catalog
        return catalog

    def _add_group_user_idempotent(self, group_id: str, user_id: str) -> None:
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
        rows = self._api.user_attribute_user_values(user_id, attribute_ids)
        values: Dict[str, Optional[str]] = {}
        for row in rows or []:
            aid = str(row.get("user_attribute_id", ""))
            if aid:
                values[aid] = (
                    None if row.get("value_is_hidden") else row.get("value")
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

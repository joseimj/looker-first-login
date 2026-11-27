"""Zero-PII idempotency ledger for deduplicating Looker login events.

Stores ONLY the numeric Looker user ID, provisioning status, an entitlement
SHA-256 digest, and timestamps—never email addresses, names, or group names.

Two implementations share the same interface:
  - InMemoryLedger: single-process atomic ledger for SMB, local dev, and tests.
  - FirestoreRestLedger: multi-replica Enterprise ledger backed by Firestore
    REST API v1 using optimistic concurrency (`currentDocument` preconditions),
    requiring zero third-party Python packages.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

import gcp_auth

DECISION_ENQUEUE = "enqueue"
DECISION_SKIP_FRESH = "skip_fresh"
DECISION_SKIP_IN_FLIGHT = "skip_in_flight"


class LedgerError(Exception):
    """Raised when the backing ledger store fails."""


@dataclasses.dataclass(frozen=True)
class LedgerRecord:
    """Zero-PII state tracked for one Looker user ID."""

    status: str
    entitlement_hash: str = ""
    lease_until: float = 0.0
    updated_at: float = 0.0


class InMemoryLedger:
    """Thread-safe zero-PII ledger for SMB deployments and unit tests."""

    def __init__(self, *, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._records: Dict[str, LedgerRecord] = {}
        self._cursor: Optional[str] = None

    def try_acquire_lease(
        self,
        user_id: str,
        *,
        fresh_ttl_s: float = 86400.0,
        lease_ttl_s: float = 60.0,
        force: bool = False,
    ) -> str:
        """Atomically decides whether a login event should be enqueued.

        Returns DECISION_ENQUEUE (lease acquired), DECISION_SKIP_FRESH (user
        was recently provisioned), or DECISION_SKIP_IN_FLIGHT (another worker
        holds an active lease).
        """
        now = self._clock()
        with self._lock:
            existing = self._records.get(user_id)
            if existing is not None:
                if existing.lease_until > now:
                    return DECISION_SKIP_IN_FLIGHT
                if (
                    not force
                    and existing.status in ("provisioned", "already_provisioned", "no_entitlements")
                    and (now - existing.updated_at) < fresh_ttl_s
                ):
                    return DECISION_SKIP_FRESH
            self._records[user_id] = LedgerRecord(
                status="in_progress",
                entitlement_hash=existing.entitlement_hash if existing else "",
                lease_until=now + lease_ttl_s,
                updated_at=existing.updated_at if existing else 0.0,
            )
            return DECISION_ENQUEUE

    def record_result(
        self, user_id: str, status: str, entitlement_hash: str = ""
    ) -> None:
        now = self._clock()
        with self._lock:
            self._records[user_id] = LedgerRecord(
                status=status,
                entitlement_hash=entitlement_hash,
                lease_until=0.0,
                updated_at=now,
            )

    def release_lease(self, user_id: str) -> None:
        """Clears an in-flight lease after an error so retries can proceed."""
        with self._lock:
            existing = self._records.get(user_id)
            if existing is not None and existing.status == "in_progress":
                self._records[user_id] = LedgerRecord(
                    status="failed",
                    entitlement_hash=existing.entitlement_hash,
                    lease_until=0.0,
                    updated_at=existing.updated_at,
                )

    def get(self, user_id: str) -> Optional[LedgerRecord]:
        with self._lock:
            return self._records.get(user_id)

    def get_reconcile_cursor(self) -> Optional[str]:
        with self._lock:
            return self._cursor

    def set_reconcile_cursor(self, cursor: str) -> None:
        with self._lock:
            self._cursor = cursor


class FirestoreRestLedger:
    """Zero-PII Enterprise ledger using Firestore REST API v1 + CAS leases."""

    def __init__(
        self,
        project_id: str,
        *,
        database: str = "(default)",
        collection: str = "looker_jit_ledger",
        token_provider: Optional[gcp_auth.MetadataTokenProvider] = None,
        api_base: str = "https://firestore.googleapis.com/v1",
        timeout_s: float = 5.0,
        clock: Callable[[], float] = time.time,
    ):
        self._project_id = project_id
        self._database = database
        self._collection = collection
        self._token_provider = token_provider or gcp_auth.MetadataTokenProvider()
        self._api_base = api_base.rstrip("/")
        self._timeout_s = timeout_s
        self._clock = clock

    def _doc_path(self, doc_id: str) -> str:
        safe_id = urllib.parse.quote(doc_id, safe="")
        return (
            f"projects/{self._project_id}/databases/{self._database}"
            f"/documents/{self._collection}/{safe_id}"
        )

    def _commit_url(self) -> str:
        return (
            f"{self._api_base}/projects/{self._project_id}/databases/"
            f"{self._database}/documents:commit"
        )

    def try_acquire_lease(
        self,
        user_id: str,
        *,
        fresh_ttl_s: float = 86400.0,
        lease_ttl_s: float = 60.0,
        force: bool = False,
    ) -> str:
        now = self._clock()
        doc_path = self._doc_path(f"user_{user_id}")
        existing, update_time = self._get_doc(doc_path)
        if existing is not None:
            if existing.lease_until > now:
                return DECISION_SKIP_IN_FLIGHT
            if (
                not force
                and existing.status
                in ("provisioned", "already_provisioned", "no_entitlements")
                and (now - existing.updated_at) < fresh_ttl_s
            ):
                return DECISION_SKIP_FRESH
            precondition: Dict[str, Any] = {"updateTime": update_time}
            ent_hash = existing.entitlement_hash
            updated_at = existing.updated_at
        else:
            precondition = {"exists": False}
            ent_hash = ""
            updated_at = 0.0

        write_payload = {
            "writes": [
                {
                    "update": {
                        "name": doc_path,
                        "fields": _encode_record(
                            LedgerRecord(
                                status="in_progress",
                                entitlement_hash=ent_hash,
                                lease_until=now + lease_ttl_s,
                                updated_at=updated_at,
                            )
                        ),
                    },
                    "currentDocument": precondition,
                }
            ]
        }
        status, body = self._request("POST", self._commit_url(), write_payload)
        if status == 200:
            return DECISION_ENQUEUE
        if status in (400, 409) and (
            "FAILED_PRECONDITION" in body or "ALREADY_EXISTS" in body
        ):
            # Another Cloud Run Gate replica won the atomic CAS race.
            return DECISION_SKIP_IN_FLIGHT
        raise LedgerError(f"Firestore lease commit failed HTTP {status}: {body[:200]}")

    def record_result(
        self, user_id: str, status: str, entitlement_hash: str = ""
    ) -> None:
        now = self._clock()
        doc_path = self._doc_path(f"user_{user_id}")
        write_payload = {
            "writes": [
                {
                    "update": {
                        "name": doc_path,
                        "fields": _encode_record(
                            LedgerRecord(
                                status=status,
                                entitlement_hash=entitlement_hash,
                                lease_until=0.0,
                                updated_at=now,
                            )
                        ),
                    }
                }
            ]
        }
        code, body = self._request("POST", self._commit_url(), write_payload)
        if code != 200:
            raise LedgerError(
                f"Firestore record_result failed HTTP {code}: {body[:200]}"
            )

    def release_lease(self, user_id: str) -> None:
        self.record_result(user_id, "failed")

    def get(self, user_id: str) -> Optional[LedgerRecord]:
        record, _ = self._get_doc(self._doc_path(f"user_{user_id}"))
        return record

    def get_reconcile_cursor(self) -> Optional[str]:
        status, body = self._request(
            "GET", f"{self._api_base}/{self._doc_path('_reconcile_state')}"
        )
        if status == 404:
            return None
        if status != 200:
            raise LedgerError(f"Firestore cursor read HTTP {status}: {body[:200]}")
        fields = json.loads(body).get("fields") or {}
        cursor = (fields.get("cursor") or {}).get("stringValue")
        return str(cursor) if cursor else None

    def set_reconcile_cursor(self, cursor: str) -> None:
        write_payload = {
            "writes": [
                {
                    "update": {
                        "name": self._doc_path("_reconcile_state"),
                        "fields": {"cursor": {"stringValue": cursor}},
                    }
                }
            ]
        }
        status, body = self._request("POST", self._commit_url(), write_payload)
        if status != 200:
            raise LedgerError(f"Firestore cursor write HTTP {status}: {body[:200]}")

    def _get_doc(self, doc_path: str) -> Tuple[Optional[LedgerRecord], Optional[str]]:
        status, body = self._request("GET", f"{self._api_base}/{doc_path}")
        if status == 404:
            return None, None
        if status != 200:
            raise LedgerError(f"Firestore GET failed HTTP {status}: {body[:200]}")
        payload = json.loads(body)
        return _decode_record(payload.get("fields") or {}), payload.get("updateTime")

    def _request(
        self, method: str, url: str, payload: Any = None
    ) -> Tuple[int, str]:
        token = self._token_provider.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_s
            ) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as error:
            raise LedgerError(f"Firestore request failed: {error}") from error


def _encode_record(record: LedgerRecord) -> Dict[str, Any]:
    return {
        "status": {"stringValue": record.status},
        "entitlement_hash": {"stringValue": record.entitlement_hash},
        "lease_until": {"doubleValue": float(record.lease_until)},
        "updated_at": {"doubleValue": float(record.updated_at)},
    }


def _decode_record(fields: Mapping[str, Any]) -> LedgerRecord:
    def _num(name: str) -> float:
        entry = fields.get(name) or {}
        if "doubleValue" in entry:
            return float(entry["doubleValue"])
        if "integerValue" in entry:
            return float(entry["integerValue"])
        return 0.0

    return LedgerRecord(
        status=str((fields.get("status") or {}).get("stringValue", "")),
        entitlement_hash=str(
            (fields.get("entitlement_hash") or {}).get("stringValue", "")
        ),
        lease_until=_num("lease_until"),
        updated_at=_num("updated_at"),
    )

"""Task dispatcher with priority queues for Enterprise Cloud Tasks & SMB inline.

Enterprise uses two isolated Cloud Tasks queues so background revocation sweeps
never starve live first-time users waiting on the splash screen:
  - PRIORITY_HIGH ("jit-onboarding-high"): live first-login provisioning.
  - PRIORITY_LOW  ("reconcile-low"):       background IGA delta revocations.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import gcp_auth

PRIORITY_HIGH = "high"
PRIORITY_LOW = "low"


class DispatchError(Exception):
    """Raised when a provisioning task cannot be enqueued."""


class InlineDispatcher:
    """Executes tasks immediately in-process (SMB / single-service / tests)."""

    def __init__(self, handler: Callable[[Dict[str, Any]], Any]):
        self._handler = handler
        self.dispatched: List[Tuple[str, Dict[str, Any]]] = []

    def enqueue(
        self, payload: Dict[str, Any], *, priority: str = PRIORITY_HIGH
    ) -> Dict[str, Any]:
        self.dispatched.append((priority, dict(payload)))
        result = self._handler(payload)
        return {"enqueued": True, "priority": priority, "inline_result": result}


class CloudTasksRestDispatcher:
    """Enqueues authenticated HTTP tasks via Cloud Tasks REST API v2."""

    def __init__(
        self,
        project_id: str,
        location: str,
        high_queue: str,
        low_queue: str,
        worker_url: str,
        *,
        oidc_service_account: Optional[str] = None,
        oidc_audience: Optional[str] = None,
        token_provider: Optional[gcp_auth.MetadataTokenProvider] = None,
        api_base: str = "https://cloudtasks.googleapis.com/v2",
        timeout_s: float = 10.0,
    ):
        self._project_id = project_id
        self._location = location
        self._queues = {PRIORITY_HIGH: high_queue, PRIORITY_LOW: low_queue}
        self._worker_url = worker_url
        self._oidc_service_account = oidc_service_account
        self._oidc_audience = oidc_audience
        self._token_provider = token_provider or gcp_auth.MetadataTokenProvider()
        self._api_base = api_base.rstrip("/")
        self._timeout_s = timeout_s

    def enqueue(
        self, payload: Dict[str, Any], *, priority: str = PRIORITY_HIGH
    ) -> Dict[str, Any]:
        queue = self._queues.get(priority, self._queues[PRIORITY_HIGH])
        url = (
            f"{self._api_base}/projects/{self._project_id}/locations/"
            f"{self._location}/queues/{queue}/tasks"
        )
        encoded_body = base64.b64encode(
            json.dumps(payload).encode("utf-8")
        ).decode("ascii")
        http_request: Dict[str, Any] = {
            "httpMethod": "POST",
            "url": self._worker_url,
            "headers": {"Content-Type": "application/json"},
            "body": encoded_body,
        }
        if self._oidc_service_account:
            oidc: Dict[str, str] = {
                "serviceAccountEmail": self._oidc_service_account
            }
            if self._oidc_audience:
                oidc["audience"] = self._oidc_audience
            http_request["oidcToken"] = oidc

        task_body = json.dumps({"task": {"httpRequest": http_request}}).encode(
            "utf-8"
        )
        token = self._token_provider.get_token()
        request = urllib.request.Request(
            url,
            data=task_body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_s
            ) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
                return {
                    "enqueued": True,
                    "priority": priority,
                    "queue": queue,
                    "task_name": data.get("name"),
                }
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            raise DispatchError(
                f"Cloud Tasks enqueue failed HTTP {error.code}: {body[:200]}"
            ) from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise DispatchError(f"Cloud Tasks enqueue failed: {error}") from error

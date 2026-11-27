"""GCP Metadata Server OAuth2 access token client (standard library only).

Used by Cloud Run services in the Enterprise profile to authenticate against
Firestore REST API v1 and Cloud Tasks REST API v2 without third-party SDK
dependencies. Accepts a static bearer token override for local testing.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)
_EXPIRY_MARGIN_S = 60.0


class GcpAuthError(Exception):
    """Raised when an access token cannot be acquired from GCP metadata."""


class MetadataTokenProvider:
    """Fetches and caches OAuth2 bearer tokens from the GCP metadata server."""

    def __init__(
        self,
        *,
        token_url: str = _METADATA_TOKEN_URL,
        static_token: Optional[str] = None,
        timeout_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._token_url = token_url
        self._static_token = (
            static_token
            if static_token is not None
            else os.environ.get("GCP_ACCESS_TOKEN")
        )
        self._timeout_s = timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at = 0.0

    def get_token(self) -> str:
        """Returns a valid OAuth2 access token."""
        if self._static_token:
            return self._static_token
        with self._lock:
            if self._token and self._clock() < self._expires_at:
                return self._token
            request = urllib.request.Request(
                self._token_url, headers={"Metadata-Flavor": "Google"}
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self._timeout_s
                ) as response:
                    payload = json.loads(
                        response.read().decode("utf-8", "replace")
                    )
            except (urllib.error.URLError, OSError, ValueError) as error:
                raise GcpAuthError(
                    f"Failed to obtain GCP metadata token: {error}"
                ) from error
            token = payload.get("access_token")
            if not token:
                raise GcpAuthError("Metadata response missing access_token")
            lifetime_s = float(payload.get("expires_in") or 300)
            self._token = token
            self._expires_at = (
                self._clock() + max(lifetime_s - _EXPIRY_MARGIN_S, 0.0)
            )
            return token

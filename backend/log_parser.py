"""Parser for Looker Core `user_login` Cloud Logging entries via Pub/Sub push.

Extracts the numeric Looker user ID from structured log fields without relying
on brittle free-text regexes, and deterministically drops events emitted by
excluded API service accounts (preventing worker self-trigger loops).

Supports both:
  1. Looker Core customer logs (`looker.googleapis.com/user_login` or
     `UserLogin` with `jsonPayload.actor.id`).
  2. Cloud Audit Logs (`cloudaudit.googleapis.com/data_access` with
     `protoPayload.methodName == "lookerapp.event.authentication.login"`).
"""

from __future__ import annotations

import base64
import dataclasses
import json
import re
from typing import Any, FrozenSet, Mapping, Optional

_USER_ID_RE = re.compile(r"^[0-9]{1,20}$")


class LogParseError(Exception):
    """Raised when a Pub/Sub push envelope or Cloud Logging entry is invalid."""


@dataclasses.dataclass(frozen=True)
class LoginEvent:
    """Normalized login event extracted from Cloud Logging."""

    user_id: str
    event_id: str = ""
    log_name: str = ""
    ignored_reason: Optional[str] = None


def parse_pubsub_push(
    envelope: Mapping[str, Any],
    *,
    excluded_actor_ids: FrozenSet[str] = frozenset(),
) -> LoginEvent:
    """Decodes a Pub/Sub push envelope carrying a Cloud Logging LogEntry."""
    message = envelope.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("data"), str):
        raise LogParseError("Pub/Sub envelope missing base64 message.data")
    try:
        raw = base64.b64decode(message["data"])
        entry = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise LogParseError(f"Invalid base64/JSON in message.data: {error}") from error
    if not isinstance(entry, dict):
        raise LogParseError("Decoded Cloud Logging entry must be a JSON object")
    return parse_log_entry(entry, excluded_actor_ids=excluded_actor_ids)


def parse_log_entry(
    entry: Mapping[str, Any],
    *,
    excluded_actor_ids: FrozenSet[str] = frozenset(),
) -> LoginEvent:
    """Extracts a LoginEvent from a Looker Core Cloud Logging LogEntry."""
    log_name = str(entry.get("logName") or "")
    json_payload = entry.get("jsonPayload")
    proto_payload = entry.get("protoPayload")

    user_id: Optional[str] = None
    event_id = ""

    if isinstance(json_payload, dict):
        actor = json_payload.get("actor")
        if isinstance(actor, dict) and actor.get("id") is not None:
            user_id = str(actor["id"]).strip()
        event_id = str(
            json_payload.get("eventId") or json_payload.get("event_id") or ""
        )
        # Looker customer log `user_login` should never trigger on login failures.
        event_name = str(
            json_payload.get("eventName") or json_payload.get("event_name") or ""
        ).lower()
        if "login_failure" in log_name.lower() or event_name == "login_failure":
            return LoginEvent(
                user_id=user_id or "",
                event_id=event_id,
                log_name=log_name,
                ignored_reason="login_failure",
            )
    elif isinstance(proto_payload, dict):
        method = str(proto_payload.get("methodName") or "")
        if method and method != "lookerapp.event.authentication.login":
            return LoginEvent(
                user_id="",
                log_name=log_name,
                ignored_reason=f"method:{method}",
            )
        auth_info = proto_payload.get("authenticationInfo") or {}
        metadata = proto_payload.get("metadata") or {}
        event_meta = metadata.get("event") if isinstance(metadata, dict) else {}
        actor_id = None
        if isinstance(event_meta, dict):
            actor_id = event_meta.get("user_id") or (
                (event_meta.get("actor") or {}).get("id")
                if isinstance(event_meta.get("actor"), dict)
                else None
            )
        if actor_id is None and isinstance(auth_info, dict):
            actor_id = auth_info.get("principalSubject")
        if actor_id is not None:
            user_id = str(actor_id).strip()

    if not user_id or not _USER_ID_RE.fullmatch(user_id):
        return LoginEvent(
            user_id="",
            event_id=event_id,
            log_name=log_name,
            ignored_reason="missing_or_invalid_actor_id",
        )
    if user_id in excluded_actor_ids:
        return LoginEvent(
            user_id=user_id,
            event_id=event_id,
            log_name=log_name,
            ignored_reason="excluded_service_account",
        )
    return LoginEvent(user_id=user_id, event_id=event_id, log_name=log_name)

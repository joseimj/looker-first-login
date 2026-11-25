"""Measures how fast a group membership added through the API takes effect.

Answers the question "does add_group_user give access immediately, or only
after a new login?" at the API layer, as a complement to the manual check in
the browser:

1. creates a temporary API key for a test user (the subject),
2. checks that the subject cannot read a probe resource (a folder or a
   dashboard that only the target group can see),
3. adds the subject to the target group with the admin credentials,
4. polls the probe with the subject's *existing* API session, and with a
   fresh login if that session never gets access,
5. removes the membership and deletes the temporary API key.

Example:

    export LOOKER_CLIENT_ID=... LOOKER_CLIENT_SECRET=...   # admin API key
    python3 verify_immediacy.py \
        --looker-base-url https://example.cloud.looker.com \
        --subject-user-id 123 --group-id 45 --probe-path /folders/67

Use a throwaway test user: the script creates and deletes an API key on that
account. Run it against a test instance.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import time
from typing import Any, Callable, Optional, Sequence

import looker_api

VERDICT_IMMEDIATE = "IMMEDIATE"
VERDICT_DELAYED = "DELAYED"
VERDICT_RELOGIN_REQUIRED = "RELOGIN_REQUIRED"
VERDICT_NOT_EFFECTIVE = "NOT_EFFECTIVE"

# Waits between probes. Few, widening probes keep the check cheap for
# editions with a small monthly API allotment.
DEFAULT_POLL_DELAYS_S = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)

_EXPLANATIONS = {
    VERDICT_IMMEDIATE: (
        "The existing API session could read the probe on the first attempt "
        "after add_group_user returned."
    ),
    VERDICT_DELAYED: (
        "The existing API session got access, but only after a delay. The "
        "extension should poll or wait before redirecting."
    ),
    VERDICT_RELOGIN_REQUIRED: (
        "The existing API session never got access, a fresh login did. "
        "Access seems to be fixed at login: a page reload may not be enough."
    ),
    VERDICT_NOT_EFFECTIVE: (
        "Neither the existing session nor a fresh login got access. Check "
        "that the group really grants access to the probe, and the SAML "
        "'Advanced Role Management' options that block membership or role "
        "inheritance from non-mirrored groups."
    ),
}


class PreconditionError(Exception):
    """The instance isn't in the state the check needs to start from."""


@dataclasses.dataclass(frozen=True)
class CheckResult:
    """Outcome of one immediacy check.

    Attributes:
        verdict: One of the VERDICT_* constants.
        existing_session_latency_s: Time between add_group_user returning and
            the existing session reading the probe, or None if it never did.
        fresh_session_has_access: Whether a fresh login could read the probe;
            None when the existing session got access (not tested).
        add_call_duration_s: Duration of the add_group_user call.
        probes: Number of probe calls made with the existing session after
            the membership was added.
    """

    verdict: str
    existing_session_latency_s: Optional[float]
    fresh_session_has_access: Optional[bool]
    add_call_duration_s: float
    probes: int


def has_access(client: looker_api.LookerApi, probe_path: str) -> bool:
    """Returns whether the client's user can read the probe resource."""
    try:
        client.request("GET", probe_path, query={"fields": "id"})
    except looker_api.LookerApiError as error:
        if error.status in (403, 404):
            return False
        raise
    return True


def run_check(
    admin: looker_api.LookerApi,
    make_client: Callable[[str, str], looker_api.LookerApi],
    subject_user_id: str,
    group_id: str,
    probe_path: str,
    *,
    poll_delays_s: Sequence[float] = DEFAULT_POLL_DELAYS_S,
    keep_membership: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda message: None,
) -> CheckResult:
    """Runs the check and always cleans up what it created.

    Args:
        admin: API client authenticated as a Looker admin.
        make_client: Builds a client from (client_id, client_secret).
        subject_user_id: Looker user ID of the test user.
        group_id: Group that grants access to the probe.
        probe_path: API path only members of the group can read, for example
            "/folders/67" or "/dashboards/89".
        poll_delays_s: Waits between successive probes.
        keep_membership: Leave the subject in the group afterwards.
        clock: Monotonic clock, injectable for tests.
        sleep: Sleep function, injectable for tests.
        log: Receives progress messages.

    Returns:
        The measured outcome.

    Raises:
        PreconditionError: If the subject is already in the group or can
            already read the probe.
        looker_api.LookerApiError: If a Looker API call fails.
    """
    subject = admin.user(subject_user_id, fields="id,group_ids")
    if str(group_id) in {str(g) for g in subject.get("group_ids") or []}:
        raise PreconditionError(
            f"User {subject_user_id} is already a member of group {group_id}."
        )

    credentials = admin.create_user_credentials_api3(subject_user_id)
    log("Created a temporary API key for the subject.")
    added = False
    try:
        existing = make_client(
            credentials["client_id"], credentials["client_secret"]
        )
        if has_access(existing, probe_path):
            raise PreconditionError(
                f"User {subject_user_id} can already read {probe_path}; pick "
                "a probe that only the target group can see."
            )
        log("Baseline confirmed: the subject cannot read the probe.")

        before_add = clock()
        admin.add_group_user(group_id, subject_user_id)
        after_add = clock()
        added = True
        log("add_group_user returned; probing with the existing session.")

        latency = None
        probes = 0
        delays = list(poll_delays_s)
        while True:
            probes += 1
            if has_access(existing, probe_path):
                latency = clock() - after_add
                break
            if not delays:
                break
            sleep(delays.pop(0))

        fresh_access = None
        if latency is None:
            log("No access with the existing session; trying a fresh login.")
            fresh = make_client(
                credentials["client_id"], credentials["client_secret"]
            )
            fresh_access = has_access(fresh, probe_path)

        if latency is not None:
            verdict = VERDICT_IMMEDIATE if probes == 1 else VERDICT_DELAYED
        elif fresh_access:
            verdict = VERDICT_RELOGIN_REQUIRED
        else:
            verdict = VERDICT_NOT_EFFECTIVE
        return CheckResult(
            verdict=verdict,
            existing_session_latency_s=latency,
            fresh_session_has_access=fresh_access,
            add_call_duration_s=after_add - before_add,
            probes=probes,
        )
    finally:
        _clean_up(
            admin,
            subject_user_id,
            group_id,
            credentials.get("id"),
            remove_membership=added and not keep_membership,
            log=log,
        )


def _clean_up(
    admin: looker_api.LookerApi,
    subject_user_id: str,
    group_id: str,
    credentials_id: Any,
    *,
    remove_membership: bool,
    log: Callable[[str], None],
) -> None:
    """Removes what the check created, reporting rather than raising."""
    if remove_membership:
        try:
            admin.delete_group_user(group_id, subject_user_id)
            log("Removed the subject from the group.")
        except looker_api.LookerApiError as error:
            log(
                f"WARNING: remove user {subject_user_id} from group "
                f"{group_id} by hand: {error}"
            )
    if credentials_id is None:
        log(
            f"WARNING: delete the temporary API key of user "
            f"{subject_user_id} by hand (no key ID was returned)."
        )
        return
    try:
        admin.delete_user_credentials_api3(subject_user_id, credentials_id)
        log("Deleted the temporary API key.")
    except looker_api.LookerApiError as error:
        log(
            f"WARNING: delete API key {credentials_id} of user "
            f"{subject_user_id} by hand: {error}"
        )


def format_report(result: CheckResult) -> str:
    """Renders the outcome for a human."""
    lines = [
        f"Verdict: {result.verdict}",
        _EXPLANATIONS[result.verdict],
        f"add_group_user call took {result.add_call_duration_s * 1000:.0f} ms.",
    ]
    if result.existing_session_latency_s is not None:
        lines.append(
            "Existing session got access "
            f"{result.existing_session_latency_s * 1000:.0f} ms after the call "
            f"returned (probe {result.probes})."
        )
    else:
        lines.append(
            f"Existing session had no access after {result.probes} probes; "
            f"fresh login had access: {result.fresh_session_has_access}."
        )
    return "\n".join(lines)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--looker-base-url", default=os.environ.get("LOOKER_BASE_URL")
    )
    parser.add_argument("--subject-user-id", required=True)
    parser.add_argument("--group-id", required=True)
    parser.add_argument(
        "--probe-path",
        required=True,
        help="API path only the group can read, e.g. /folders/67.",
    )
    parser.add_argument(
        "--poll-delays-s",
        default=",".join(str(delay) for delay in DEFAULT_POLL_DELAYS_S),
        help="Comma-separated waits between probes, in seconds.",
    )
    parser.add_argument("--keep-membership", action="store_true")
    args = parser.parse_args(argv)
    if not args.looker_base_url:
        parser.error("--looker-base-url or LOOKER_BASE_URL is required")
    if not args.probe_path.startswith("/"):
        parser.error("--probe-path must start with '/'")
    try:
        args.poll_delays = [
            float(delay) for delay in args.poll_delays_s.split(",") if delay
        ]
    except ValueError:
        parser.error("--poll-delays-s must be comma-separated numbers")
    return args


def main(argv: Sequence[str]) -> int:
    args = _parse_args(argv)
    client_id = os.environ.get("LOOKER_CLIENT_ID")
    client_secret = os.environ.get("LOOKER_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "Set LOOKER_CLIENT_ID and LOOKER_CLIENT_SECRET to an admin API key.",
            file=sys.stderr,
        )
        return 2
    base_url = args.looker_base_url
    admin = looker_api.LookerApi(base_url, client_id, client_secret)
    try:
        result = run_check(
            admin,
            lambda key, secret: looker_api.LookerApi(base_url, key, secret),
            args.subject_user_id,
            args.group_id,
            args.probe_path,
            poll_delays_s=args.poll_delays,
            keep_membership=args.keep_membership,
            log=print,
        )
    except PreconditionError as error:
        print(f"Cannot start: {error}", file=sys.stderr)
        return 2
    except looker_api.LookerApiError as error:
        print(f"Looker API call failed: {error}", file=sys.stderr)
        return 2
    print()
    print(format_report(result))
    return 0 if result.verdict in (VERDICT_IMMEDIATE, VERDICT_DELAYED) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

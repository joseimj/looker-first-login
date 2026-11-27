"""Tests for Enterprise modules: log_parser, ledger (in-memory & Firestore REST),
task_dispatcher (inline & Cloud Tasks REST), and HttpIgaEntitlements."""

import base64
import http.server
import json
import threading
import unittest

import entitlements as entitlements_lib
import fake_looker
import gcp_auth
import ledger as ledger_lib
import log_parser
import task_dispatcher


def _pubsub_envelope(log_entry):
    encoded = base64.b64encode(json.dumps(log_entry).encode("utf-8")).decode(
        "ascii"
    )
    return {"message": {"data": encoded, "messageId": "m-1"}, "subscription": "sub"}


class _StubServer:
    """Tiny local HTTP stub capturing requests and answering from a callback."""

    def __init__(self, handler_fn):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _run(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                status, resp = handler_fn(self.command, self.path, body)
                data = json.dumps(resp).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _run

            def log_message(self, format, *args):  # pylint: disable=redefined-builtin
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


class LogParserTest(unittest.TestCase):

    def test_parses_customer_json_payload_user_login(self):
        envelope = _pubsub_envelope({
            "logName": "projects/p/logs/looker.googleapis.com%2Fuser_login",
            "jsonPayload": {
                "actor": {"id": "101", "email": ""},
                "eventId": "ev-99",
                "eventName": "login",
            },
        })

        event = log_parser.parse_pubsub_push(
            envelope, excluded_actor_ids=frozenset({"1"})
        )

        self.assertEqual(event.user_id, "101")
        self.assertEqual(event.event_id, "ev-99")
        self.assertIsNone(event.ignored_reason)

    def test_excludes_provisioning_service_account_to_prevent_loop(self):
        envelope = _pubsub_envelope({
            "logName": "projects/p/logs/looker.googleapis.com%2Fuser_login",
            "jsonPayload": {"actor": {"id": "1"}, "eventName": "login"},
        })

        event = log_parser.parse_pubsub_push(
            envelope, excluded_actor_ids=frozenset({"1"})
        )

        self.assertEqual(event.ignored_reason, "excluded_service_account")

    def test_ignores_login_failure_events(self):
        envelope = _pubsub_envelope({
            "logName": "projects/p/logs/looker.googleapis.com%2Fuser_login_failure",
            "jsonPayload": {"actor": {"id": "101"}, "eventName": "login_failure"},
        })

        event = log_parser.parse_pubsub_push(envelope)

        self.assertEqual(event.ignored_reason, "login_failure")

    def test_parses_cloud_audit_proto_payload_login(self):
        envelope = _pubsub_envelope({
            "logName": "projects/p/logs/cloudaudit.googleapis.com%2Fdata_access",
            "protoPayload": {
                "methodName": "lookerapp.event.authentication.login",
                "metadata": {"event": {"user_id": "205"}},
            },
        })

        event = log_parser.parse_pubsub_push(envelope)

        self.assertEqual(event.user_id, "205")
        self.assertIsNone(event.ignored_reason)

    def test_rejects_malformed_envelope(self):
        with self.assertRaises(log_parser.LogParseError):
            log_parser.parse_pubsub_push({"message": {"data": "!!!not-b64!!!"}})


class LedgerTest(unittest.TestCase):

    def test_in_memory_ledger_lease_freshness_and_release(self):
        clock = fake_looker.FakeClock(start=1000.0)
        led = ledger_lib.InMemoryLedger(clock=clock)

        self.assertEqual(led.try_acquire_lease("101"), ledger_lib.DECISION_ENQUEUE)
        # Concurrent duplicate login while lease is held.
        self.assertEqual(
            led.try_acquire_lease("101"), ledger_lib.DECISION_SKIP_IN_FLIGHT
        )
        led.record_result("101", "provisioned", "abc123")
        # Repeat login within fresh TTL -> skipped with 0 Looker API calls.
        self.assertEqual(
            led.try_acquire_lease("101", fresh_ttl_s=3600),
            ledger_lib.DECISION_SKIP_FRESH,
        )
        # After TTL expires -> re-enqueued.
        clock.sleep(3601)
        self.assertEqual(
            led.try_acquire_lease("101", fresh_ttl_s=3600),
            ledger_lib.DECISION_ENQUEUE,
        )
        # Release on transient error allows immediate retry.
        led.release_lease("101")
        self.assertEqual(led.try_acquire_lease("101"), ledger_lib.DECISION_ENQUEUE)

    def test_firestore_rest_ledger_atomic_cas_and_zero_pii(self):
        docs = {}
        clock = fake_looker.FakeClock(start=2000.0)

        def handle(method, path, body):
            if method == "GET":
                doc_name = path.lstrip("/")
                if doc_name not in docs:
                    return 404, {"error": {"status": "NOT_FOUND"}}
                return 200, docs[doc_name]
            payload = json.loads(body)
            write = payload["writes"][0]
            doc_name = write["update"]["name"]
            precond = write.get("currentDocument") or {}
            if precond.get("exists") is False and doc_name in docs:
                return 400, {"error": {"status": "FAILED_PRECONDITION"}}
            if "updateTime" in precond and (
                doc_name not in docs
                or docs[doc_name]["updateTime"] != precond["updateTime"]
            ):
                return 400, {"error": {"status": "FAILED_PRECONDITION"}}
            docs[doc_name] = {
                "name": doc_name,
                "fields": write["update"]["fields"],
                "updateTime": f"2026-11-27T00:00:{int(clock()) % 60:02d}Z",
            }
            return 200, {"writeResults": [{}]}

        stub = _StubServer(handle)
        self.addCleanup(stub.close)
        led = ledger_lib.FirestoreRestLedger(
            "demo-proj",
            token_provider=gcp_auth.MetadataTokenProvider(static_token="tok"),
            api_base=stub.base_url,
            clock=clock,
        )

        self.assertEqual(led.try_acquire_lease("101"), ledger_lib.DECISION_ENQUEUE)
        # Verify zero PII fields in Firestore document.
        stored = next(iter(docs.values()))["fields"]
        self.assertEqual(
            set(stored.keys()),
            {"status", "entitlement_hash", "lease_until", "updated_at"},
        )
        self.assertEqual(
            led.try_acquire_lease("101"), ledger_lib.DECISION_SKIP_IN_FLIGHT
        )
        led.record_result("101", "provisioned", "hash-1")
        self.assertEqual(
            led.try_acquire_lease("101", fresh_ttl_s=86400),
            ledger_lib.DECISION_SKIP_FRESH,
        )
        led.set_reconcile_cursor("cur-42")
        self.assertEqual(led.get_reconcile_cursor(), "cur-42")


class CloudTasksAndIgaTest(unittest.TestCase):

    def test_cloud_tasks_rest_dispatches_to_high_and_low_queues_with_oidc(self):
        stub = _StubServer(lambda m, p, b: (200, {"name": f"{p}/task-1"}))
        self.addCleanup(stub.close)
        disp = task_dispatcher.CloudTasksRestDispatcher(
            "demo-proj",
            "europe-west3",
            "jit-onboarding-high",
            "reconcile-low",
            "https://worker.run.app/tasks/provision",
            oidc_service_account="tasks-sa@demo-proj.iam.gserviceaccount.com",
            oidc_audience="https://worker.run.app",
            token_provider=gcp_auth.MetadataTokenProvider(static_token="tok"),
            api_base=stub.base_url,
        )

        hi = disp.enqueue({"user_id": "101"}, priority=task_dispatcher.PRIORITY_HIGH)
        lo = disp.enqueue({"user_id": "102", "revoke": True}, priority=task_dispatcher.PRIORITY_LOW)

        self.assertEqual(hi["queue"], "jit-onboarding-high")
        self.assertEqual(lo["queue"], "reconcile-low")
        self.assertIn("/queues/jit-onboarding-high/tasks", stub.requests[0][1])
        self.assertIn("/queues/reconcile-low/tasks", stub.requests[1][1])
        task_req = json.loads(stub.requests[0][3])["task"]["httpRequest"]
        self.assertEqual(
            task_req["oidcToken"]["serviceAccountEmail"],
            "tasks-sa@demo-proj.iam.gserviceaccount.com",
        )
        self.assertEqual(
            json.loads(base64.b64decode(task_req["body"])), {"user_id": "101"}
        )

    def test_http_iga_entitlements_lookup_and_change_feed(self):
        def handle(method, path, body):
            del method, body
            if path.startswith("/entitlements?email=alice%40example.com"):
                return 200, {
                    "groups": ["BI_Finance_EMEA"],
                    "landing_page": "/dashboards/12",
                }
            if path.startswith("/entitlements"):
                return 404, {}
            if path.startswith("/changes"):
                return 200, {
                    "emails": ["alice@example.com", "bob@example.com"],
                    "next_cursor": "2026-11-27T04:00:00Z",
                }
            return 404, {}

        stub = _StubServer(handle)
        self.addCleanup(stub.close)
        iga = entitlements_lib.HttpIgaEntitlements(
            stub.base_url, bearer_token="iga-secret"
        )

        alice = iga.lookup("Alice@Example.com")
        missing = iga.lookup("nobody@example.com")
        emails, cursor = iga.changed_emails(since="2026-11-26T00:00:00Z")

        self.assertEqual(alice.groups, ("BI_Finance_EMEA",))
        self.assertEqual(len(alice.digest()), 64)
        self.assertIsNone(missing)
        self.assertEqual(emails, ["alice@example.com", "bob@example.com"])
        self.assertEqual(cursor, "2026-11-27T04:00:00Z")


if __name__ == "__main__":
    unittest.main()

"""End-to-end HTTP server tests covering BOTH SMB and Enterprise profiles."""

import base64
import http.client
import json
import logging
import threading
import unittest

import entitlements as entitlements_lib
import fake_looker
import ledger as ledger_lib
import looker_api
import provisioner
import server
import task_dispatcher

LOOKER_ORIGIN = "https://example.cloud.looker.com"


class StaticEntitlements:
    def __init__(self, by_email, changed=()):
        self._by_email = by_email
        self._changed = list(changed)

    def lookup(self, email):
        return self._by_email.get(email)

    def changed_emails(self, since=None):
        del since
        return list(self._changed), "cursor-next"


def _pubsub_login(user_id, event_id="ev-1"):
    entry = {
        "logName": "projects/p/logs/looker.googleapis.com%2Fuser_login",
        "jsonPayload": {
            "actor": {"id": str(user_id)},
            "eventId": event_id,
            "eventName": "login",
        },
    }
    return {
        "message": {
            "data": base64.b64encode(json.dumps(entry).encode("utf-8")).decode(
                "ascii"
            )
        }
    }


class ServerTest(unittest.TestCase):

    def setUp(self):
        super().setUp()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.fake = fake_looker.FakeLooker()
        fake_looker.seed_demo(self.fake)
        looker = fake_looker.FakeLookerServer(self.fake)
        self.addCleanup(looker.close)
        self.api = looker_api.LookerApi(
            looker.base_url, "demo-admin-id", "demo-admin-secret"
        )
        self.clock = fake_looker.FakeClock(start=5000.0)
        self.entitlements = StaticEntitlements(
            {
                "alice@example.com": entitlements_lib.Entitlement(
                    groups=("BI_Finance_EMEA", "BI_Dashboard_Hub"),
                    landing_page="/dashboards/12",
                )
            },
            changed=["alice@example.com", "carol@example.com"],
        )
        self.ledger = ledger_lib.InMemoryLedger(clock=self.clock)
        self.prov = provisioner.Provisioner(
            self.api,
            self.entitlements,
            set_landing_page=False,
            pending_group_name="BI_Access_Pending_Approval",
            managed_group_prefix="BI_",
            ledger=self.ledger,
        )
        self.dispatcher = task_dispatcher.InlineDispatcher(
            lambda task: self.prov.provision(
                task["user_id"], revoke=bool(task.get("revoke", False))
            )
        )
        self.app = server.App(
            provisioner=self.prov,
            api=self.api,
            entitlements=self.entitlements,
            ledger=self.ledger,
            dispatcher=self.dispatcher,
            profile="all",
            allowed_origins=frozenset({LOOKER_ORIGIN}),
            excluded_actor_ids=frozenset({"1"}),
            clock=self.clock,
        )
        httpd = server.ProvisioningServer(("127.0.0.1", 0), self.app)
        thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        if body is not None and not isinstance(body, (bytes, str)):
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, resp.headers, json.loads(raw) if raw else None

    def test_smb_direct_provision_cors_secret_and_chunked(self):
        self.app.shared_secret = "s3cret"
        status, _, _ = self.request("POST", "/provision", {"user_id": "101"})
        self.assertEqual(status, 401)

        payload = json.dumps({"user_id": "101", "secret": "s3cret"}).encode()
        chunked = f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
        status, headers, body = self.request(
            "POST",
            "/provision",
            body=chunked,
            headers={
                "Origin": LOOKER_ORIGIN,
                "Transfer-Encoding": "chunked",
                "Content-Type": "application/json",
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], LOOKER_ORIGIN)
        self.assertEqual(
            body, {"status": "provisioned", "redirect": "/dashboards/12"}
        )
        self.assertEqual(self.fake.group_ids_of("101"), ["1", "10", "20", "21"])

    def test_enterprise_gate_provisions_once_then_drops_repeat_logins_at_zero_api_cost(self):
        status, _, body = self.request(
            "POST", "/events/user-login", _pubsub_login("101")
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "enqueued")
        self.assertEqual(self.fake.group_ids_of("101"), ["1", "10", "20", "21"])

        calls_after_first = len(self.fake.calls)
        status2, _, body2 = self.request(
            "POST", "/events/user-login", _pubsub_login("101", event_id="ev-2")
        )
        self.assertEqual(status2, 200)
        self.assertEqual(body2["action"], "skip_fresh")
        self.assertEqual(len(self.fake.calls), calls_after_first)

    def test_enterprise_gate_ignores_provisioning_service_account_login(self):
        status, _, body = self.request(
            "POST", "/events/user-login", _pubsub_login("1")
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "ignored")
        self.assertEqual(body["reason"], "excluded_service_account")
        self.assertEqual(self.fake.calls, [])

    def test_enterprise_reconcile_uses_low_priority_queue_and_revokes_stale_groups(self):
        # Give Alice a stale managed group BI_Marketing_Global (22).
        self.fake.add_user(
            "101",
            "alice@example.com",
            group_ids=["1", "10", "20", "22"],
            saml_email="alice@example.com",
        )
        status, _, body = self.request("POST", "/tasks/reconcile", {})

        self.assertEqual(status, 200)
        self.assertEqual(body["changed_emails"], 2)
        self.assertEqual(body["enqueued"], 2)
        self.assertEqual(body["next_cursor"], "cursor-next")
        self.assertEqual(self.ledger.get_reconcile_cursor(), "cursor-next")
        # Both dispatched on low priority queue.
        self.assertTrue(
            all(p == task_dispatcher.PRIORITY_LOW for p, _ in self.dispatcher.dispatched)
        )
        # Alice lost stale BI_Marketing_Global (22) but kept All Users (1) & SAML (10).
        self.assertEqual(self.fake.group_ids_of("101"), ["1", "10", "20", "21"])
        # Carol (unentitled) received pending marker (99).
        self.assertIn("99", self.fake.group_ids_of("103"))

    def test_profile_isolation_blocks_disallowed_routes(self):
        self.app.profile = "smb"
        status, _, _ = self.request("POST", "/events/user-login", _pubsub_login("101"))
        self.assertEqual(status, 404)

        self.app.profile = "worker"
        status, _, _ = self.request("POST", "/provision", {"user_id": "101"})
        self.assertEqual(status, 404)
        status, _, body = self.request("POST", "/tasks/provision", {"user_id": "101"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "provisioned")


if __name__ == "__main__":
    unittest.main()

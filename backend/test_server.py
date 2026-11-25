"""Tests for the HTTP front end, with the fake Looker behind it."""

import http.client
import json
import logging
import os
import threading
import unittest
from unittest import mock

import fake_looker
import looker_api
import provisioner
import server

LOOKER_ORIGIN = "https://example.cloud.looker.com"


class StaticEntitlements:
    """Entitlements held in memory, keyed by lower-case email."""

    def __init__(self, by_email):
        self._by_email = by_email

    def lookup(self, email):
        return self._by_email.get(email)


class ServerTest(unittest.TestCase):

    def setUp(self):
        super().setUp()
        # The server logs every rejected call; keep the test output readable.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.fake = fake_looker.FakeLooker()
        self.fake.add_user("1", "admin@example.com", admin=True)
        self.fake.add_api_key("1", "admin-id", "admin-secret")
        self.fake.add_user("101", "alice@example.com", group_ids=["10"])
        self.fake.add_user("102", "bob@example.com", group_ids=["10"])
        self.fake.add_group("10", "SAML - BI Users")
        self.fake.add_group("20", "Finance - EMEA")
        self.fake.add_user_attribute("1", "landing_page")
        looker = fake_looker.FakeLookerServer(self.fake)
        self.addCleanup(looker.close)
        self.api = looker_api.LookerApi(
            looker.base_url, "admin-id", "admin-secret"
        )
        self.clock = fake_looker.FakeClock()
        self.app = server.App(
            provisioner=provisioner.Provisioner(
                self.api,
                StaticEntitlements(
                    {
                        "alice@example.com": provisioner.Entitlement(
                            groups=("Finance - EMEA",),
                            landing_page="/dashboards/12",
                        )
                    }
                ),
            ),
            allowed_origins=frozenset({LOOKER_ORIGIN}),
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
        """Returns (status, headers, decoded JSON body or None)."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        if body is not None and not isinstance(body, (bytes, str)):
            body = json.dumps(body)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        return response.status, response.headers, json.loads(raw) if raw else None

    def provision(self, body, headers=None):
        return self.request("POST", "/provision", body, headers)

    def test_provision_returns_only_status_and_redirect(self):
        status, _, body = self.provision({"user_id": "101"})

        self.assertEqual(status, 200)
        self.assertEqual(
            body, {"status": "provisioned", "redirect": "/dashboards/12"}
        )
        self.assertEqual(self.fake.group_ids_of("101"), ["10", "20"])

    def test_debug_response_includes_the_details(self):
        self.app.debug_response = True

        _, _, body = self.provision({"user_id": "101"})

        self.assertEqual(body["groups_added"], ["Finance - EMEA"])
        self.assertTrue(body["landing_page_set"])

    def test_user_without_entitlements_gets_no_redirect(self):
        status, _, body = self.provision({"user_id": "102"})

        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "no_entitlements", "redirect": None})

    def test_numeric_user_id_is_accepted(self):
        status, _, body = self.provision({"user_id": 101})

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "provisioned")

    def test_allowed_origin_is_echoed_in_cors_header(self):
        status, headers, _ = self.provision(
            {"user_id": "101"}, {"Origin": LOOKER_ORIGIN}
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], LOOKER_ORIGIN)
        self.assertEqual(headers["Vary"], "Origin")

    def test_other_origin_is_rejected_without_calling_looker(self):
        status, headers, _ = self.provision(
            {"user_id": "101"}, {"Origin": "https://evil.example.com"}
        )

        self.assertEqual(status, 403)
        self.assertIsNone(headers["Access-Control-Allow-Origin"])
        self.assertEqual(self.fake.calls, [])

    def test_wildcard_allows_any_origin(self):
        self.app.allowed_origins = frozenset({"*"})

        status, headers, _ = self.provision(
            {"user_id": "101"}, {"Origin": "https://other.example.com"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            headers["Access-Control-Allow-Origin"], "https://other.example.com"
        )

    def test_preflight_from_allowed_origin_permits_the_post(self):
        status, headers, body = self.request(
            "OPTIONS",
            "/provision",
            headers={
                "Origin": LOOKER_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
                "Access-Control-Request-Private-Network": "true",
            },
        )

        self.assertEqual(status, 204)
        self.assertIsNone(body)
        self.assertEqual(headers["Access-Control-Allow-Origin"], LOOKER_ORIGIN)
        self.assertEqual(headers["Access-Control-Allow-Methods"], "POST, OPTIONS")
        self.assertEqual(
            headers["Access-Control-Allow-Headers"],
            "Content-Type, X-Provisioning-Secret",
        )
        self.assertEqual(headers["Access-Control-Allow-Private-Network"], "true")

    def test_preflight_from_other_origin_is_rejected(self):
        status, headers, _ = self.request(
            "OPTIONS",
            "/provision",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(status, 403)
        self.assertIsNone(headers["Access-Control-Allow-Origin"])

    def test_call_without_shared_secret_is_rejected(self):
        self.app.shared_secret = "s3cret"
        for body in (
            {"user_id": "101"},
            {"user_id": "101", "secret": "wrong"},
            # What arrives when Looker didn't resolve the secret key tag.
            {"user_id": "101", "secret": "{{provisioning_landing_splash_key}}"},
            {"user_id": "101", "secret": 12345},
        ):
            with self.subTest(body=body):
                status, _, _ = self.provision(body)

                self.assertEqual(status, 401)
        self.assertEqual(self.fake.calls, [])

    def test_shared_secret_is_accepted_in_the_body(self):
        self.app.shared_secret = "s3cret"

        status, _, _ = self.provision({"user_id": "101", "secret": "s3cret"})

        self.assertEqual(status, 200)

    def test_shared_secret_is_accepted_in_the_header(self):
        self.app.shared_secret = "s3cret"

        status, _, _ = self.provision(
            {"user_id": "101"}, {"X-Provisioning-Secret": "s3cret"}
        )

        self.assertEqual(status, 200)

    def test_invalid_user_id_is_rejected(self):
        for user_id in ("", "12; drop", "../1", None, True, 1.5, ["101"]):
            with self.subTest(user_id=user_id):
                status, _, body = self.provision({"user_id": user_id})

                self.assertEqual(status, 400)
                self.assertEqual(body["message"], "Invalid user_id.")
        self.assertEqual(self.fake.calls, [])

    def test_malformed_body_is_rejected(self):
        for body in ("{not json", "[1, 2]", '"101"', ""):
            with self.subTest(body=body):
                status, _, _ = self.provision(body)

                self.assertEqual(status, 400)

    def test_oversized_body_is_rejected(self):
        status, _, _ = self.provision({"user_id": "101", "padding": "x" * 20000})

        self.assertEqual(status, 413)
        self.assertEqual(self.fake.calls, [])

    def test_unknown_path_is_not_found(self):
        status, _, _ = self.request("POST", "/other", {"user_id": "101"})

        self.assertEqual(status, 404)

    def test_health_check_answers_ok(self):
        status, _, body = self.request("GET", "/healthz")

        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_looker_failure_is_reported_without_internals(self):
        self.fake.fail_next = [500]

        status, _, body = self.provision({"user_id": "101"})

        self.assertEqual(status, 502)
        self.assertEqual(
            body, {"status": "error", "message": "Looker API call failed."}
        )

    def test_misconfiguration_is_reported_without_internals(self):
        self.app.provisioner = provisioner.Provisioner(
            self.api,
            StaticEntitlements(
                {
                    "alice@example.com": provisioner.Entitlement(
                        groups=("No Such Group",)
                    )
                }
            ),
        )

        status, _, body = self.provision({"user_id": "101"})

        self.assertEqual(status, 500)
        self.assertEqual(
            body,
            {"status": "error", "message": "Provisioning is misconfigured."},
        )

    def test_recent_success_is_replayed_without_calling_looker(self):
        self.provision({"user_id": "101"})
        calls_after_first = list(self.fake.calls)

        status, _, body = self.provision({"user_id": "101"})

        self.assertEqual(status, 200)
        self.assertEqual(
            body, {"status": "provisioned", "redirect": "/dashboards/12"}
        )
        self.assertEqual(self.fake.calls, calls_after_first)

    def test_replayed_result_reports_that_it_cost_no_looker_call(self):
        self.app.debug_response = True
        _, _, first = self.provision({"user_id": "101"})

        _, _, replay = self.provision({"user_id": "101"})

        self.assertEqual(first["admin_api_calls"], 5)
        self.assertNotIn("cached", first)
        self.assertIs(replay["cached"], True)
        self.assertEqual(replay["admin_api_calls"], 0)

    def test_looker_is_asked_again_once_the_cached_result_expired(self):
        self.provision({"user_id": "101"})
        calls_after_first = len(self.fake.calls)
        self.clock.sleep(301)

        _, _, body = self.provision({"user_id": "101"})

        self.assertEqual(body["status"], "already_provisioned")
        self.assertEqual(len(self.fake.calls), calls_after_first + 2)

    def test_missing_entitlements_are_not_cached(self):
        self.provision({"user_id": "102"})
        calls_after_first = len(self.fake.calls)

        self.provision({"user_id": "102"})

        self.assertEqual(len(self.fake.calls), calls_after_first + 1)

    def test_chunked_request_body_is_accepted(self):
        payload = json.dumps({"user_id": "101"}).encode("utf-8")
        chunked = (
            f"{len(payload):x}\r\n".encode("ascii")
            + payload
            + b"\r\n0\r\n\r\n"
        )
        status, _, body = self.request(
            "POST",
            "/provision",
            body=chunked,
            headers={
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "provisioned")

    def test_oversized_chunked_request_body_is_rejected(self):
        payload = b"x" * 12000
        chunked = (
            f"{len(payload):x}\r\n".encode("ascii")
            + payload
            + b"\r\n0\r\n\r\n"
        )
        status, _, _ = self.request(
            "POST",
            "/provision",
            body=chunked,
            headers={
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )

        self.assertEqual(status, 413)
        self.assertEqual(self.fake.calls, [])

    def test_normalize_origins_strips_trailing_slashes_and_merges_env(self):
        with mock.patch.dict(
            os.environ,
            {
                "ALLOWED_ORIGINS": (
                    "https://a.example.com/ , https://b.example.com"
                )
            },
            clear=False,
        ):
            origins = server._normalize_origins(["https://c.example.com/"])

        self.assertEqual(
            origins,
            frozenset({
                "https://a.example.com",
                "https://b.example.com",
                "https://c.example.com",
            }),
        )


if __name__ == "__main__":
    unittest.main()

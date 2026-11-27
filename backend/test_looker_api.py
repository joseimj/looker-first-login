"""Tests for looker_api.LookerApi against the in-memory fake Looker."""

import socket
import unittest

import fake_looker
import looker_api


class LookerApiTest(unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.clock = fake_looker.FakeClock()
        self.fake = fake_looker.FakeLooker(clock=self.clock)
        self.fake.add_user("1", "admin@example.com", admin=True)
        self.fake.add_api_key("1", "admin-id", "admin-secret")
        server = fake_looker.FakeLookerServer(self.fake)
        self.addCleanup(server.close)
        self.base_url = server.base_url
        self.sleeps = []
        self.api = looker_api.LookerApi(
            self.base_url,
            "admin-id",
            "admin-secret",
            sleep=self.sleeps.append,
            clock=self.clock,
        )

    def test_logs_in_once_and_reuses_token_within_50m_window(self):
        self.api.me()
        self.clock.sleep(3000 - 1)  # 50 minutes - 1s
        self.api.me()

        self.assertEqual(self.fake.login_count, 1)

    def test_refreshes_token_after_50m_window(self):
        self.api.me()
        self.clock.sleep(3000 + 1)  # past 50m margin on 60m token
        self.api.me()

        self.assertEqual(self.fake.login_count, 2)

    def test_reauthenticates_on_401(self):
        self.api.me()
        self.fake.expire_tokens()

        user = self.api.me(fields="id")

        self.assertEqual(user, {"id": "1"})
        self.assertEqual(self.fake.login_count, 2)

    def test_retries_429_respecting_retry_after(self):
        self.fake.fail_next = [429]
        self.fake.retry_after_s = 7

        self.api.me()

        self.assertEqual(self.sleeps, [7.0])
        self.assertEqual(self.api.call_count, 2)

    def test_does_not_retry_404(self):
        with self.assertRaises(looker_api.LookerApiError) as raised:
            self.api.user("999", fields="id")

        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(self.sleeps, [])

    def test_connection_failure_raises_status_zero(self):
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
        api = looker_api.LookerApi(f"http://127.0.0.1:{port}", "id", "secret")

        with self.assertRaises(looker_api.LookerApiError) as raised:
            api.me()

        self.assertEqual(raised.exception.status, 0)

    def test_user_attribute_values_and_group_search_and_user_search(self):
        self.fake.add_user("101", "alice@example.com")
        self.fake.add_group("20", "BI_Finance_EMEA")
        self.fake.add_user_attribute("2", "region")
        self.api.set_user_attribute_user_value("101", "2", "EMEA")

        rows = self.api.user_attribute_user_values("101", ["2"])
        users = self.api.search_users_by_email("alice@example.com")
        groups = self.api.all_groups()

        self.assertEqual(rows[0]["value"], "EMEA")
        self.assertEqual([u["id"] for u in users], ["101"])
        self.assertEqual([g["name"] for g in groups], ["BI_Finance_EMEA"])

    def test_rejects_plain_http_remote_and_invalid_concurrency(self):
        with self.assertRaises(ValueError):
            looker_api.LookerApi("http://looker.example.com", "id", "secret")
        with self.assertRaises(ValueError):
            looker_api.LookerApi(self.base_url, "id", "secret", max_concurrency=0)


if __name__ == "__main__":
    unittest.main()

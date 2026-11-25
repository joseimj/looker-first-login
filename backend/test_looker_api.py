"""Tests for looker_api.LookerApi, run against the in-memory fake Looker."""

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

    def test_returns_decoded_response(self):
        user = self.api.me(fields="id,email")

        self.assertEqual(user, {"id": "1", "email": "admin@example.com"})

    def test_logs_in_once_for_several_calls(self):
        self.api.me()
        self.api.me()

        self.assertEqual(self.fake.login_count, 1)
        self.assertEqual(self.api.login_count, 1)

    def test_logs_in_again_when_token_is_rejected(self):
        self.api.me()
        self.fake.expire_tokens()

        user = self.api.me(fields="id")

        self.assertEqual(user, {"id": "1"})
        self.assertEqual(self.fake.login_count, 2)

    def test_logs_in_again_shortly_before_token_lifetime_ends(self):
        self.api.me()
        self.clock.sleep(3600 - 59)  # The fake issues one-hour tokens.

        self.api.me()

        self.assertEqual(self.fake.login_count, 2)

    def test_keeps_token_while_it_is_valid(self):
        self.api.me()
        self.clock.sleep(3600 - 61)

        self.api.me()

        self.assertEqual(self.fake.login_count, 1)

    def test_retries_throttled_call_and_counts_every_attempt(self):
        self.fake.fail_next = [429]

        user = self.api.me(fields="id")

        self.assertEqual(user, {"id": "1"})
        self.assertEqual(self.sleeps, [0.5])
        self.assertEqual(self.api.call_count, 2)

    def test_waits_as_long_as_retry_after_asks(self):
        self.fake.fail_next = [429]
        self.fake.retry_after_s = 7

        self.api.me()

        self.assertEqual(self.sleeps, [7.0])

    def test_gives_up_after_max_retries(self):
        self.fake.fail_next = [503, 503, 503, 503]

        with self.assertRaises(looker_api.LookerApiError) as raised:
            self.api.me()

        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(self.sleeps, [0.5, 1.0, 2.0])
        self.assertEqual(self.api.call_count, 4)

    def test_does_not_retry_not_found(self):
        with self.assertRaises(looker_api.LookerApiError) as raised:
            self.api.user("999", fields="id")

        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(raised.exception.path, "/users/999")
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.api.call_count, 1)

    def test_wrong_credentials_fail_at_login(self):
        api = looker_api.LookerApi(self.base_url, "admin-id", "wrong-secret")

        with self.assertRaises(looker_api.LookerApiError) as raised:
            api.me()

        self.assertEqual(raised.exception.path, "/login")
        self.assertEqual(api.call_count, 0)

    def test_connection_failure_is_reported_with_status_zero(self):
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            closed_port = unused.getsockname()[1]
        api = looker_api.LookerApi(
            f"http://127.0.0.1:{closed_port}", "admin-id", "admin-secret"
        )

        with self.assertRaises(looker_api.LookerApiError) as raised:
            api.me()

        self.assertEqual(raised.exception.status, 0)

    def test_add_group_user_adds_the_user_to_the_group(self):
        self.fake.add_user("101", "alice@example.com")
        self.fake.add_group("20", "Finance - EMEA")

        self.api.add_group_user("20", "101")

        self.assertEqual(self.fake.group_ids_of("101"), ["20"])

    def test_delete_group_user_removes_the_user_from_the_group(self):
        self.fake.add_user("101", "alice@example.com", group_ids=["20"])
        self.fake.add_group("20", "Finance - EMEA")

        self.api.delete_group_user("20", "101")

        self.assertEqual(self.fake.group_ids_of("101"), [])

    def test_set_user_attribute_user_value_stores_the_value(self):
        self.fake.add_user("101", "alice@example.com")
        self.fake.add_user_attribute("2", "region")

        self.api.set_user_attribute_user_value("101", "2", "EMEA")

        self.assertEqual(self.fake.attribute_values, {("101", "2"): "EMEA"})

    def test_user_attribute_user_values_filters_by_attribute_ids(self):
        self.fake.add_user("101", "alice@example.com")
        self.fake.add_user_attribute("1", "landing_page")
        self.fake.add_user_attribute("2", "region")
        self.api.set_user_attribute_user_value("101", "1", "/dashboards/12")
        self.api.set_user_attribute_user_value("101", "2", "EMEA")

        rows = self.api.user_attribute_user_values("101", ["2"])

        self.assertEqual(
            rows,
            [
                {
                    "user_attribute_id": "2",
                    "name": "region",
                    "value": "EMEA",
                    "value_is_hidden": False,
                    "source": "User",
                }
            ],
        )

    def test_rejects_non_positive_max_concurrency(self):
        with self.assertRaises(ValueError):
            looker_api.LookerApi(
                self.base_url, "admin-id", "admin-secret", max_concurrency=0
            )

    def test_rejects_plain_http_for_remote_hosts(self):
        with self.assertRaises(ValueError):
            looker_api.LookerApi("http://looker.example.com", "id", "secret")

    def test_accepts_https_url_with_trailing_slash(self):
        api = looker_api.LookerApi("https://looker.example.com/", "id", "secret")

        self.assertEqual(api.call_count, 0)


if __name__ == "__main__":
    unittest.main()

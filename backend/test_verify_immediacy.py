"""Tests for verify_immediacy against the in-memory fake Looker."""

import unittest

import fake_looker
import looker_api
import verify_immediacy


class VerifyImmediacyTest(unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.clock = fake_looker.FakeClock()
        self.fake = fake_looker.FakeLooker(clock=self.clock)
        fake_looker.seed_demo(self.fake)
        server = fake_looker.FakeLookerServer(self.fake)
        self.addCleanup(server.close)
        self.base_url = server.base_url
        self.admin = looker_api.LookerApi(
            self.base_url, "demo-admin-id", "demo-admin-secret"
        )

    def _run(self, **kwargs):
        return verify_immediacy.run_check(
            self.admin,
            lambda cid, sec: looker_api.LookerApi(self.base_url, cid, sec),
            "102",
            "20",
            "/folders/67",
            clock=self.clock,
            sleep=self.clock.sleep,
            **kwargs,
        )

    def test_immediate_delayed_relogin_and_cleanup(self):
        res = self._run()
        self.assertEqual(res.verdict, "IMMEDIATE")
        self.assertNotIn("20", self.fake.group_ids_of("102"))
        self.assertEqual(self.fake.api_key_count("102"), 0)

        self.fake.membership_delay_s = 2.0
        res2 = self._run()
        self.assertEqual(res2.verdict, "DELAYED")

        self.fake.membership_delay_s = 0.0
        self.fake.snapshot_groups_at_login = True
        res3 = self._run()
        self.assertEqual(res3.verdict, "RELOGIN_REQUIRED")
        self.assertIn("redirectMode: 'logout'", verify_immediacy.format_report(res3))


if __name__ == "__main__":
    unittest.main()

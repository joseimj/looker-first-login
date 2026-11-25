"""Tests for the immediacy check, run against the in-memory fake Looker."""

import contextlib
import io
import os
import unittest
from unittest import mock

import fake_looker
import looker_api
import verify_immediacy


class RunCheckTest(unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.clock = fake_looker.FakeClock()
        self.fake = fake_looker.FakeLooker(clock=self.clock)
        self.fake.add_user("1", "admin@example.com", admin=True)
        self.fake.add_api_key("1", "admin-id", "admin-secret")
        self.fake.add_user("200", "subject@example.com")
        self.fake.add_group("20", "Finance - EMEA")
        self.fake.add_group("21", "Dashboard Hub Viewers")
        self.fake.add_folder("67", ["20"])
        server = fake_looker.FakeLookerServer(self.fake)
        self.addCleanup(server.close)
        self.base_url = server.base_url
        self.admin = looker_api.LookerApi(
            self.base_url, "admin-id", "admin-secret"
        )

    def run_check(self, **kwargs):
        return verify_immediacy.run_check(
            self.admin,
            lambda key, secret: looker_api.LookerApi(self.base_url, key, secret),
            "200",
            "20",
            "/folders/67",
            clock=self.clock,
            sleep=self.clock.sleep,
            **kwargs,
        )

    def test_immediate_when_membership_is_effective_at_once(self):
        result = self.run_check()

        self.assertEqual(result.verdict, "IMMEDIATE")
        self.assertEqual(result.probes, 1)
        self.assertEqual(result.existing_session_latency_s, 0.0)
        self.assertIsNone(result.fresh_session_has_access)

    def test_delayed_when_membership_takes_time_to_apply(self):
        self.fake.membership_delay_s = 3.0

        result = self.run_check()

        # Probes at 0 s, 0.5 s and 1.5 s fail; the one at 3.5 s succeeds.
        self.assertEqual(result.verdict, "DELAYED")
        self.assertEqual(result.probes, 4)
        self.assertEqual(result.existing_session_latency_s, 3.5)

    def test_relogin_required_when_session_keeps_its_login_time_groups(self):
        self.fake.snapshot_groups_at_login = True

        result = self.run_check()

        self.assertEqual(result.verdict, "RELOGIN_REQUIRED")
        self.assertIsNone(result.existing_session_latency_s)
        self.assertIs(result.fresh_session_has_access, True)
        self.assertEqual(result.probes, 7)

    def test_not_effective_when_group_does_not_grant_access(self):
        self.fake.add_folder("67", ["21"])

        result = self.run_check()

        self.assertEqual(result.verdict, "NOT_EFFECTIVE")
        self.assertIs(result.fresh_session_has_access, False)

    def test_custom_poll_delays_bound_the_number_of_probes(self):
        self.fake.add_folder("67", ["21"])

        result = self.run_check(poll_delays_s=[1.0, 1.0])

        self.assertEqual(result.probes, 3)

    def test_removes_membership_and_temporary_api_key_afterwards(self):
        self.run_check()

        self.assertEqual(self.fake.group_ids_of("200"), [])
        self.assertEqual(self.fake.api_key_count("200"), 0)

    def test_keep_membership_leaves_the_subject_in_the_group(self):
        self.run_check(keep_membership=True)

        self.assertEqual(self.fake.group_ids_of("200"), ["20"])
        self.assertEqual(self.fake.api_key_count("200"), 0)

    def test_refuses_subject_already_in_the_group(self):
        self.fake.add_user("200", "subject@example.com", group_ids=["20"])

        with self.assertRaisesRegex(
            verify_immediacy.PreconditionError, "already a member"
        ):
            self.run_check()

        self.assertEqual(self.fake.group_ids_of("200"), ["20"])
        self.assertEqual(self.fake.api_key_count("200"), 0)

    def test_refuses_subject_who_can_already_read_the_probe(self):
        self.fake.add_user("200", "subject@example.com", group_ids=["21"])
        self.fake.add_folder("67", ["20", "21"])

        with self.assertRaisesRegex(
            verify_immediacy.PreconditionError, "can already read"
        ):
            self.run_check()

        self.assertEqual(self.fake.group_ids_of("200"), ["21"])
        self.assertEqual(self.fake.api_key_count("200"), 0)

    def test_deletes_temporary_api_key_when_adding_to_the_group_fails(self):
        with self.assertRaises(looker_api.LookerApiError):
            verify_immediacy.run_check(
                self.admin,
                lambda key, secret: looker_api.LookerApi(
                    self.base_url, key, secret
                ),
                "200",
                "no-such-group",
                "/folders/67",
                clock=self.clock,
                sleep=self.clock.sleep,
            )

        self.assertEqual(self.fake.api_key_count("200"), 0)

    def test_reports_progress_through_the_log_callback(self):
        messages = []

        self.run_check(log=messages.append)

        self.assertEqual(
            messages,
            [
                "Created a temporary API key for the subject.",
                "Baseline confirmed: the subject cannot read the probe.",
                "add_group_user returned; probing with the existing session.",
                "Removed the subject from the group.",
                "Deleted the temporary API key.",
            ],
        )


class FormatReportTest(unittest.TestCase):

    def test_report_for_immediate_access(self):
        result = verify_immediacy.CheckResult(
            verdict="IMMEDIATE",
            existing_session_latency_s=0.25,
            fresh_session_has_access=None,
            add_call_duration_s=0.12,
            probes=1,
        )

        report = verify_immediacy.format_report(result)

        self.assertIn("Verdict: IMMEDIATE", report)
        self.assertIn("add_group_user call took 120 ms.", report)
        self.assertIn("got access 250 ms after the call returned", report)

    def test_report_when_a_fresh_login_was_needed(self):
        result = verify_immediacy.CheckResult(
            verdict="RELOGIN_REQUIRED",
            existing_session_latency_s=None,
            fresh_session_has_access=True,
            add_call_duration_s=0.1,
            probes=7,
        )

        report = verify_immediacy.format_report(result)

        self.assertIn("Verdict: RELOGIN_REQUIRED", report)
        self.assertIn(
            "no access after 7 probes; fresh login had access: True.", report
        )


class MainTest(unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.fake = fake_looker.FakeLooker()
        self.fake.add_user("1", "admin@example.com", admin=True)
        self.fake.add_api_key("1", "admin-id", "admin-secret")
        self.fake.add_user("200", "subject@example.com")
        self.fake.add_group("20", "Finance - EMEA")
        self.fake.add_folder("67", ["20"])
        server = fake_looker.FakeLookerServer(self.fake)
        self.addCleanup(server.close)
        self.argv = [
            "--looker-base-url", server.base_url,
            "--subject-user-id", "200",
            "--group-id", "20",
            "--probe-path", "/folders/67",
            "--poll-delays-s", "0",
        ]
        self.env = {
            "LOOKER_CLIENT_ID": "admin-id",
            "LOOKER_CLIENT_SECRET": "admin-secret",
        }

    def run_main(self, env):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            with contextlib.redirect_stdout(stdout):
                with contextlib.redirect_stderr(stderr):
                    code = verify_immediacy.main(self.argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_exits_zero_when_access_is_effective(self):
        code, stdout, _ = self.run_main(self.env)

        self.assertEqual(code, 0)
        self.assertIn("Verdict: IMMEDIATE", stdout)

    def test_exits_one_when_access_is_not_effective(self):
        self.fake.add_folder("67", ["99"])

        code, stdout, _ = self.run_main(self.env)

        self.assertEqual(code, 1)
        self.assertIn("Verdict: NOT_EFFECTIVE", stdout)

    def test_exits_two_without_admin_credentials(self):
        code, _, stderr = self.run_main({})

        self.assertEqual(code, 2)
        self.assertIn("LOOKER_CLIENT_ID", stderr)

    def test_exits_two_when_precondition_is_not_met(self):
        self.fake.add_user("200", "subject@example.com", group_ids=["20"])

        code, _, stderr = self.run_main(self.env)

        self.assertEqual(code, 2)
        self.assertIn("already a member", stderr)


if __name__ == "__main__":
    unittest.main()

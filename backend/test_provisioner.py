"""Tests for fail-closed delta provisioning, marker groups, and safe revocation."""

import threading
import unittest

import entitlements as entitlements_lib
import fake_looker
import ledger as ledger_lib
import looker_api
import provisioner

ALICE = entitlements_lib.Entitlement(
    groups=("BI_Finance_EMEA", "BI_Dashboard_Hub"),
    user_attributes={"region": "EMEA"},
    landing_page="/dashboards/12",
)


class StaticEntitlements:
    def __init__(self, by_email, default=None):
        self._by_email = by_email
        self._default = default

    def lookup(self, email):
        return self._by_email.get(email, self._default)


class ProvisionerTest(unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.fake = fake_looker.FakeLooker()
        fake_looker.seed_demo(self.fake)
        server = fake_looker.FakeLookerServer(self.fake)
        self.addCleanup(server.close)
        self.api = looker_api.LookerApi(
            server.base_url, "demo-admin-id", "demo-admin-secret"
        )

    def make_provisioner(self, by_email=None, **kwargs):
        if by_email is None:
            by_email = {"alice@example.com": ALICE}
        return provisioner.Provisioner(
            self.api, StaticEntitlements(by_email), **kwargs
        )

    def test_fail_closed_write_order_attributes_groups_landing_page(self):
        result = self.make_provisioner().provision("101")

        self.assertEqual(
            self.fake.writes(),
            [
                ("PATCH", "/users/101/attribute_values/2"),
                ("POST", "/groups/20/users"),
                ("POST", "/groups/21/users"),
                ("PATCH", "/users/101/attribute_values/1"),
            ],
        )
        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(result["redirect"], "/dashboards/12")
        self.assertEqual(
            result["groups_added"], ["BI_Finance_EMEA", "BI_Dashboard_Hub"]
        )
        self.assertTrue(result["landing_page_set"])

    def test_group_ranked_mode_skips_landing_page_write(self):
        result = self.make_provisioner(set_landing_page=False).provision("101")

        self.assertFalse(result["landing_page_set"])
        self.assertNotIn(("101", "1"), self.fake.attribute_values)
        self.assertEqual(result["redirect"], "/dashboards/12")

    def test_self_heals_missing_landing_page_after_partial_crash(self):
        self.fake.add_user(
            "101",
            "alice@example.com",
            group_ids=["1", "10", "20", "21"],
            saml_email="alice@example.com",
        )
        self.fake.attribute_values[("101", "2")] = "EMEA"

        result = self.make_provisioner().provision("101")

        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(result["groups_added"], [])
        self.assertEqual(result["attributes_set"], [])
        self.assertTrue(result["landing_page_set"])
        self.assertEqual(
            self.fake.writes(), [("PATCH", "/users/101/attribute_values/1")]
        )

    def test_no_entitlements_assigns_pending_marker_group(self):
        # Carol (103) is not in entitlements.
        led = ledger_lib.InMemoryLedger()
        subject = self.make_provisioner(
            pending_group_name="BI_Access_Pending_Approval",
            ledger=led,
        )

        result = subject.provision("103")

        self.assertEqual(result["status"], "no_entitlements")
        self.assertTrue(result["pending_marker_set"])
        self.assertIn("99", self.fake.group_ids_of("103"))
        self.assertEqual(led.get("103").status, "no_entitlements")

    def test_pending_marker_group_is_removed_once_entitlements_are_granted(self):
        self.fake.add_user(
            "101",
            "alice@example.com",
            group_ids=["1", "10", "99"],
            saml_email="alice@example.com",
        )
        subject = self.make_provisioner(
            pending_group_name="BI_Access_Pending_Approval",
            set_landing_page=False,
        )

        result = subject.provision("101")

        self.assertEqual(result["status"], "provisioned")
        self.assertNotIn("99", self.fake.group_ids_of("101"))
        self.assertIn("BI_Access_Pending_Approval", result["groups_removed"])

    def test_delta_revocation_only_removes_managed_prefix_groups(self):
        # Alice has All Users (1), SAML_BI_Users (10, externally managed),
        # BI_Finance_EMEA (20), BI_Marketing_Global (22 - stale!).
        self.fake.add_user(
            "101",
            "alice@example.com",
            group_ids=["1", "10", "20", "22"],
            saml_email="alice@example.com",
        )
        subject = self.make_provisioner(
            managed_group_prefix="BI_",
            set_landing_page=False,
        )

        result = subject.provision("101", revoke=True)

        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(result["groups_added"], ["BI_Dashboard_Hub"])
        self.assertEqual(result["groups_removed"], ["BI_Marketing_Global"])
        # All Users (1) and SAML_BI_Users (10) MUST remain untouched.
        self.assertEqual(self.fake.group_ids_of("101"), ["1", "10", "20", "21"])

    def test_full_revocation_when_user_loses_all_entitlements(self):
        self.fake.add_user(
            "103",
            "carol@example.com",
            group_ids=["1", "10", "20", "22"],
            saml_email="carol@example.com",
        )
        subject = self.make_provisioner(
            managed_group_prefix="BI_",
            pending_group_name="BI_Access_Pending_Approval",
        )

        result = subject.provision("103", revoke=True)

        self.assertEqual(result["status"], "no_entitlements")
        self.assertEqual(
            sorted(result["groups_removed"]),
            ["BI_Finance_EMEA", "BI_Marketing_Global"],
        )
        self.assertEqual(sorted(self.fake.group_ids_of("103")), ["1", "10", "99"])

    def test_service_account_and_non_saml_are_skipped(self):
        self.fake.add_user("200", "alice@example.com", group_ids=["1"])
        subject = self.make_provisioner(require_saml=True)

        self.assertEqual(subject.provision("1")["status"], "disabled")
        self.assertEqual(subject.provision("200")["status"], "not_saml")
        self.assertEqual(self.fake.writes(), [])

    def test_tolerates_409_conflict_across_replicas(self):
        self.fake.conflict_on_duplicate_group = True
        orig = self.api.add_group_user

        def raced_add(gid, uid):
            orig(gid, uid)
            return orig(gid, uid)

        self.api.add_group_user = raced_add
        result = self.make_provisioner().provision("101")

        self.assertEqual(result["status"], "provisioned")

    def test_concurrent_calls_for_same_user_add_each_group_once(self):
        subject = self.make_provisioner(set_landing_page=False)
        threads = [
            threading.Thread(target=subject.provision, args=("101",))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(
            self.fake.writes().count(("POST", "/groups/20/users")), 1
        )
        self.assertEqual(
            self.fake.writes().count(("POST", "/groups/21/users")), 1
        )

    def test_missing_group_fails_before_any_write(self):
        bad = entitlements_lib.Entitlement(
            groups=("BI_Finance_EMEA", "BI_Does_Not_Exist"),
            user_attributes={"region": "EMEA"},
        )
        with self.assertRaises(provisioner.ConfigError):
            self.make_provisioner({"alice@example.com": bad}).provision("101")
        self.assertEqual(self.fake.writes(), [])


if __name__ == "__main__":
    unittest.main()

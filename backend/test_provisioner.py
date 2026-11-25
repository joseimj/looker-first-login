"""Tests for the provisioning logic, run against the in-memory fake Looker."""

import json
import os
import tempfile
import threading
import unittest

import fake_looker
import looker_api
import provisioner

ALICE = provisioner.Entitlement(
    groups=("Finance - EMEA", "Dashboard Hub Viewers"),
    user_attributes={"region": "EMEA"},
    landing_page="/dashboards/12",
)


class StaticEntitlements:
    """Entitlements held in memory, keyed by lower-case email."""

    def __init__(self, by_email, default=None):
        self._by_email = by_email
        self._default = default

    def lookup(self, email):
        return self._by_email.get(email, self._default)


class ProvisionerTest(unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.fake = fake_looker.FakeLooker()
        self.fake.add_user("1", "admin@example.com", admin=True)
        self.fake.add_api_key("1", "admin-id", "admin-secret")
        self.fake.add_user("101", "alice@example.com", group_ids=["10"])
        self.fake.add_group("10", "SAML - BI Users")
        self.fake.add_group("20", "Finance - EMEA")
        self.fake.add_group("21", "Dashboard Hub Viewers")
        self.fake.add_user_attribute("1", "landing_page")
        self.fake.add_user_attribute("2", "region")
        server = fake_looker.FakeLookerServer(self.fake)
        self.addCleanup(server.close)
        self.api = looker_api.LookerApi(
            server.base_url, "admin-id", "admin-secret"
        )

    def make_provisioner(self, by_email=None, **kwargs):
        if by_email is None:
            by_email = {"alice@example.com": ALICE}
        return provisioner.Provisioner(
            self.api, StaticEntitlements(by_email), **kwargs
        )

    def test_new_user_gets_attributes_then_groups_then_landing_page(self):
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
        self.assertEqual(self.fake.group_ids_of("101"), ["10", "20", "21"])
        self.assertEqual(
            self.fake.attribute_values,
            {("101", "2"): "EMEA", ("101", "1"): "/dashboards/12"},
        )
        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(result["redirect"], "/dashboards/12")
        self.assertEqual(
            result["groups_added"], ["Finance - EMEA", "Dashboard Hub Viewers"]
        )
        self.assertEqual(result["attributes_set"], ["region"])
        self.assertTrue(result["landing_page_set"])

    def test_first_provisioning_reports_its_admin_api_calls(self):
        result = self.make_provisioner().provision("101")

        # 1 user read, 2 group lookups, 1 attribute list, then 4 writes.
        self.assertEqual(result["admin_api_calls"], 8)

    def test_provisioned_user_triggers_no_writes_on_repeat_call(self):
        subject = self.make_provisioner()
        subject.provision("101")
        writes_after_first_run = self.fake.writes()

        result = subject.provision("101")

        self.assertEqual(result["status"], "already_provisioned")
        self.assertEqual(result["redirect"], "/dashboards/12")
        # 1 user read + 1 attribute_values read to confirm nothing is missing.
        self.assertEqual(result["admin_api_calls"], 2)
        self.assertEqual(self.fake.writes(), writes_after_first_run)

    def test_provisioned_groups_only_user_costs_one_read_and_no_write(self):
        entitlement = provisioner.Entitlement(groups=("Finance - EMEA",))
        subject = self.make_provisioner(
            {"alice@example.com": entitlement}, set_landing_page=False
        )
        subject.provision("101")
        writes_after_first_run = self.fake.writes()

        result = subject.provision("101")

        self.assertEqual(result["status"], "already_provisioned")
        self.assertEqual(result["admin_api_calls"], 1)
        self.assertEqual(self.fake.writes(), writes_after_first_run)

    def test_repairs_missing_landing_page_after_interrupted_run(self):
        # Simulate a prior run that set RLS and groups, then crashed before
        # updating landing_page.
        self.fake.add_user(
            "101", "alice@example.com", group_ids=["10", "20", "21"]
        )
        self.fake.attribute_values[("101", "2")] = "EMEA"

        result = self.make_provisioner().provision("101")

        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(result["attributes_set"], [])
        self.assertEqual(result["groups_added"], [])
        self.assertTrue(result["landing_page_set"])
        self.assertEqual(
            self.fake.writes(), [("PATCH", "/users/101/attribute_values/1")]
        )

    def test_repairs_missing_rls_attribute_when_groups_already_assigned(self):
        self.fake.add_user(
            "101", "alice@example.com", group_ids=["10", "20", "21"]
        )

        result = self.make_provisioner().provision("101")

        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(result["attributes_set"], ["region"])
        self.assertEqual(result["groups_added"], [])
        self.assertTrue(result["landing_page_set"])
        self.assertEqual(
            self.fake.attribute_values,
            {("101", "2"): "EMEA", ("101", "1"): "/dashboards/12"},
        )

    def test_reapplies_hidden_attribute_when_groups_already_assigned(self):
        self.fake.add_user_attribute("3", "secret_rls", hidden=True)
        self.fake.add_user("101", "alice@example.com", group_ids=["10", "20"])
        self.fake.attribute_values[("101", "3")] = "tier-1"
        entitlement = provisioner.Entitlement(
            groups=("Finance - EMEA",), user_attributes={"secret_rls": "tier-1"}
        )
        subject = self.make_provisioner(
            {"alice@example.com": entitlement}, set_landing_page=False
        )

        result = subject.provision("101")

        # Looker masks hidden values on read, so the provisioner re-applies.
        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(result["attributes_set"], ["secret_rls"])

    def test_service_account_is_left_untouched(self):
        self.fake.add_user(
            "101", "alice@example.com", service_account=True
        )

        result = self.make_provisioner().provision("101")

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.fake.writes(), [])

    def test_falls_back_to_saml_email_when_top_level_email_is_blank(self):
        self.fake.add_user(
            "101", "", group_ids=["10"], saml_email="alice@example.com"
        )

        result = self.make_provisioner().provision("101")

        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(self.fake.group_ids_of("101"), ["10", "20", "21"])

    def test_require_saml_skips_non_saml_and_disabled_saml_accounts(self):
        self.fake.add_user("101", "alice@example.com", group_ids=["10"])
        self.fake.add_user(
            "102",
            "alice@example.com",
            group_ids=["10"],
            saml_email="alice@example.com",
            saml_disabled=True,
        )
        self.fake.add_user(
            "103",
            "alice@example.com",
            group_ids=["10"],
            saml_email="alice@example.com",
        )
        subject = self.make_provisioner(require_saml=True)

        self.assertEqual(subject.provision("101")["status"], "not_saml")
        self.assertEqual(subject.provision("102")["status"], "not_saml")
        self.assertEqual(subject.provision("103")["status"], "provisioned")
        self.assertEqual(self.fake.group_ids_of("101"), ["10"])
        self.assertEqual(self.fake.group_ids_of("102"), ["10"])
        self.assertEqual(self.fake.group_ids_of("103"), ["10", "20", "21"])

    def test_tolerates_409_conflict_when_another_replica_added_the_group(self):
        self.fake.conflict_on_duplicate_group = True
        original_add = self.api.add_group_user

        def add_after_race(group_id, user_id):
            # Simulate another replica adding the user just before our POST.
            original_add(group_id, user_id)
            return original_add(group_id, user_id)

        self.api.add_group_user = add_after_race

        result = self.make_provisioner().provision("101")

        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(self.fake.group_ids_of("101"), ["10", "20", "21"])

    def test_only_missing_groups_are_added(self):
        self.fake.add_user("101", "alice@example.com", group_ids=["10", "20"])

        result = self.make_provisioner().provision("101")

        self.assertEqual(result["groups_added"], ["Dashboard Hub Viewers"])
        self.assertEqual(self.fake.group_ids_of("101"), ["10", "20", "21"])

    def test_user_without_entitlements_is_left_untouched(self):
        self.fake.add_user("102", "bob@example.com", group_ids=["10"])

        result = self.make_provisioner().provision("102")

        self.assertEqual(result["status"], "no_entitlements")
        self.assertIsNone(result["redirect"])
        self.assertEqual(self.fake.writes(), [])

    def test_default_entitlement_applies_to_unlisted_user(self):
        self.fake.add_user("102", "bob@example.com", group_ids=["10"])
        default = provisioner.Entitlement(groups=("Dashboard Hub Viewers",))
        subject = provisioner.Provisioner(
            self.api, StaticEntitlements({}, default=default)
        )

        result = subject.provision("102")

        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(self.fake.group_ids_of("102"), ["10", "21"])

    def test_unknown_user_is_reported(self):
        result = self.make_provisioner().provision("999")

        self.assertEqual(result["status"], "unknown_user")
        self.assertEqual(self.fake.writes(), [])

    def test_disabled_user_is_left_untouched(self):
        self.fake.add_user("101", "alice@example.com", disabled=True)

        result = self.make_provisioner().provision("101")

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.fake.writes(), [])

    def test_email_from_looker_is_matched_case_insensitively(self):
        self.fake.add_user("101", "Alice@Example.COM", group_ids=["10"])

        result = self.make_provisioner().provision("101")

        self.assertEqual(result["status"], "provisioned")

    def test_missing_group_fails_before_any_write(self):
        entitlement = provisioner.Entitlement(
            groups=("Finance - EMEA", "No Such Group"),
            user_attributes={"region": "EMEA"},
        )
        subject = self.make_provisioner({"alice@example.com": entitlement})

        with self.assertRaisesRegex(
            provisioner.ConfigError, "group not found: 'No Such Group'"
        ):
            subject.provision("101")

        self.assertEqual(self.fake.writes(), [])

    def test_missing_user_attribute_fails_before_any_write(self):
        entitlement = provisioner.Entitlement(
            groups=("Finance - EMEA",), user_attributes={"no_such_attr": "x"}
        )
        subject = self.make_provisioner({"alice@example.com": entitlement})

        with self.assertRaisesRegex(
            provisioner.ConfigError, "attribute not found: 'no_such_attr'"
        ):
            subject.provision("101")

        self.assertEqual(self.fake.writes(), [])

    def test_group_name_is_matched_exactly_despite_like_wildcards(self):
        # "_" is a wildcard in the group search, so "Team_A" also finds
        # "TeamXA"; only the exact name may be used.
        self.fake.add_group("30", "TeamXA")
        self.fake.add_group("31", "Team_A")
        entitlement = provisioner.Entitlement(groups=("Team_A",))
        subject = self.make_provisioner({"alice@example.com": entitlement})

        subject.provision("101")

        self.assertEqual(self.fake.group_ids_of("101"), ["10", "31"])

    def test_dry_run_reports_the_plan_without_writing(self):
        result = self.make_provisioner(dry_run=True).provision("101")

        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(result["status"], "provisioned")
        self.assertTrue(result["dry_run"])
        self.assertEqual(
            result["groups_added"], ["Finance - EMEA", "Dashboard Hub Viewers"]
        )
        self.assertEqual(result["attributes_set"], ["region"])

    def test_landing_page_is_only_returned_when_storing_is_disabled(self):
        result = self.make_provisioner(set_landing_page=False).provision("101")

        self.assertEqual(result["redirect"], "/dashboards/12")
        self.assertFalse(result["landing_page_set"])
        self.assertNotIn(("101", "1"), self.fake.attribute_values)

    def test_entitlement_without_groups_still_sets_attributes(self):
        entitlement = provisioner.Entitlement(user_attributes={"region": "EMEA"})
        subject = self.make_provisioner({"alice@example.com": entitlement})

        result = subject.provision("101")

        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(self.fake.attribute_values, {("101", "2"): "EMEA"})

    def test_group_and_attribute_ids_are_looked_up_once(self):
        self.fake.add_user("102", "bob@example.com", group_ids=["10"])
        subject = self.make_provisioner(
            {"alice@example.com": ALICE, "bob@example.com": ALICE}
        )

        subject.provision("101")
        subject.provision("102")

        lookups = [
            call
            for call in self.fake.calls
            if call in (("GET", "/groups/search"), ("GET", "/user_attributes"))
        ]
        self.assertEqual(
            lookups,
            [
                ("GET", "/groups/search"),
                ("GET", "/groups/search"),
                ("GET", "/user_attributes"),
            ],
        )

    def test_concurrent_calls_for_one_user_add_each_group_once(self):
        subject = self.make_provisioner()
        threads = [
            threading.Thread(target=subject.provision, args=("101",))
            for _ in range(4)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(
            self.fake.writes().count(("POST", "/groups/20/users")), 1
        )
        self.assertEqual(
            self.fake.writes().count(("POST", "/groups/21/users")), 1
        )


class EntitlementsFileTest(unittest.TestCase):

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "entitlements.json")

    def write(self, data, mtime):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.utime(self.path, (mtime, mtime))

    def test_lookup_returns_the_entry_of_the_email(self):
        self.write(
            {
                "users": {
                    "Alice@Example.com": {
                        "groups": ["Finance - EMEA"],
                        "user_attributes": {"region": "EMEA"},
                        "landing_page": "/dashboards/12",
                    }
                }
            },
            mtime=1000,
        )

        entitlement = provisioner.EntitlementsFile(self.path).lookup(
            "alice@example.com"
        )

        self.assertEqual(
            entitlement,
            provisioner.Entitlement(
                groups=("Finance - EMEA",),
                user_attributes={"region": "EMEA"},
                landing_page="/dashboards/12",
            ),
        )

    def test_lookup_returns_none_without_entry_or_default(self):
        self.write({"users": {}, "default": None}, mtime=1000)

        entitlement = provisioner.EntitlementsFile(self.path).lookup(
            "bob@example.com"
        )

        self.assertIsNone(entitlement)

    def test_lookup_falls_back_to_the_default_entry(self):
        self.write(
            {"users": {}, "default": {"groups": ["Dashboard Hub Viewers"]}},
            mtime=1000,
        )

        entitlement = provisioner.EntitlementsFile(self.path).lookup(
            "bob@example.com"
        )

        self.assertEqual(
            entitlement,
            provisioner.Entitlement(groups=("Dashboard Hub Viewers",)),
        )

    def test_lookup_sees_changes_made_to_the_file(self):
        self.write({"users": {}}, mtime=1000)
        entitlements = provisioner.EntitlementsFile(self.path)
        self.write(
            {"users": {"bob@example.com": {"groups": ["Finance - EMEA"]}}},
            mtime=2000,
        )

        entitlement = entitlements.lookup("bob@example.com")

        self.assertEqual(
            entitlement, provisioner.Entitlement(groups=("Finance - EMEA",))
        )

    def test_rejects_landing_page_on_another_host(self):
        for landing_page in (
            "https://evil.example.com/",
            "//evil.example.com/",
            "/\\evil.example.com/",
            "dashboards/12",
        ):
            with self.subTest(landing_page=landing_page):
                self.write(
                    {"users": {"a@example.com": {"landing_page": landing_page}}},
                    mtime=1000,
                )

                with self.assertRaises(ValueError):
                    provisioner.EntitlementsFile(self.path)

    def test_rejects_groups_that_are_not_a_list_of_names(self):
        self.write(
            {"users": {"a@example.com": {"groups": "Finance - EMEA"}}},
            mtime=1000,
        )

        with self.assertRaises(ValueError):
            provisioner.EntitlementsFile(self.path)


if __name__ == "__main__":
    unittest.main()

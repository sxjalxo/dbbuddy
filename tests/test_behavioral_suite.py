"""Tests for the schema-adaptive BehavioralTestSuite.

These validate that the suite (a) classifies any schema into semantic roles and
(b) verifies the engine's intent using the schema's own names — so the same
behaviors pass on completely different schemas without hard-coded table names.
"""

import unittest

from dbbuddy_core.behavioral_test_suite import (
    BehavioralTestSuite,
    SchemaProfile,
    classify_schema,
    ROLE_ENTITY,
    ROLE_MONETARY,
    ROLE_EVENT,
)

DEMO_SCHEMA = {
    "users": ["id", "name", "email", "country", "signup_date"],
    "payments": ["id", "user_id", "amount", "payment_method", "payment_date"],
    "subscriptions": ["id", "user_id", "plan", "price", "status", "start_date", "end_date"],
    "feature_usage": ["id", "user_id", "feature_name", "usage_count", "last_used"],
    "sessions": ["id", "user_id", "device", "browser", "session_duration", "created_at"],
}

# Deliberately different table/column names to prove schema adaptivity.
ALT_SCHEMA = {
    "customers": ["id", "name", "email", "region", "joined"],
    "invoices": ["id", "customer_id", "amount", "status", "invoice_date"],
}


class TestSchemaClassification(unittest.TestCase):
    def test_demo_roles(self):
        roles = classify_schema(DEMO_SCHEMA)
        self.assertIn(ROLE_ENTITY, roles["users"])
        self.assertIn(ROLE_MONETARY, roles["payments"])
        self.assertIn(ROLE_EVENT, roles["payments"])

    def test_alt_roles_adapt(self):
        # No "users"/"payments" names — roles must be inferred structurally.
        roles = classify_schema(ALT_SCHEMA)
        self.assertIn(ROLE_ENTITY, roles["customers"])
        self.assertIn(ROLE_MONETARY, roles["invoices"])
        self.assertIn(ROLE_EVENT, roles["invoices"])

    def test_profile_picks_representatives(self):
        profile = SchemaProfile(ALT_SCHEMA)
        self.assertEqual(profile.entity_table(), "customers")
        self.assertEqual(profile.monetary_table(), "invoices")
        self.assertEqual(profile.monetary_column(), "amount")
        self.assertEqual(profile.event_table(), "invoices")


class TestBehavioralSuite(unittest.TestCase):
    def test_demo_all_behaviors_pass(self):
        result = BehavioralTestSuite().run(DEMO_SCHEMA)
        self.assertEqual(result["failed"], 0, [c for c in result["cases"] if c["status"] == "failed"])
        self.assertGreater(result["passed"], 0)

    def test_alt_schema_all_behaviors_pass(self):
        # Same behaviors, different schema — must still pass (adaptivity).
        result = BehavioralTestSuite().run(ALT_SCHEMA)
        self.assertEqual(result["failed"], 0, [c for c in result["cases"] if c["status"] == "failed"])
        self.assertGreater(result["passed"], 0)

    def test_behaviors_skip_when_roles_absent(self):
        # A schema with neither monetary nor entity roles should skip, not fail.
        minimal = {"logs": ["id", "message", "created_at"]}
        result = BehavioralTestSuite().run(minimal)
        self.assertEqual(result["failed"], 0)
        self.assertGreaterEqual(result["skipped"], 1)


if __name__ == "__main__":
    unittest.main()

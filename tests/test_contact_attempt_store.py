from __future__ import annotations

import tempfile
import unittest
from zoneinfo import ZoneInfoNotFoundError

from src.api.contact_attempt_store import JsonContactAttemptStore


class ContactAttemptStoreTests(unittest.TestCase):
    def test_append_and_count_attempts_for_local_day(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonContactAttemptStore(root_dir=tmpdir)
            account_ref = "acct_5001"

            store.append_event(
                account_ref=account_ref,
                decision_code="call_initialized",
                counts_toward_attempt=True,
                recorded_at_utc="2026-02-08T15:00:00+00:00",  # 09:00 America/Chicago
            )
            store.append_event(
                account_ref=account_ref,
                decision_code="blocked_policy_min_gap",
                counts_toward_attempt=False,
                recorded_at_utc="2026-02-08T15:10:00+00:00",
            )
            store.append_event(
                account_ref=account_ref,
                decision_code="call_initialized",
                counts_toward_attempt=True,
                recorded_at_utc="2026-02-09T03:00:00+00:00",  # 21:00 America/Chicago (still Feb 8 local)
            )

            attempts = store.count_attempts_for_local_day(
                account_ref=account_ref,
                timezone_name="America/Chicago",
                local_day_iso="2026-02-08",
            )
            self.assertEqual(attempts, 2)

            last_attempt = store.get_last_counted_attempt_at_utc(account_ref=account_ref)
            self.assertEqual(last_attempt, "2026-02-09T03:00:00+00:00")

    def test_list_recent_events_merges_accounts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonContactAttemptStore(root_dir=tmpdir)
            store.append_event(
                account_ref="acct_a",
                decision_code="call_initialized",
                counts_toward_attempt=True,
                recorded_at_utc="2026-02-08T10:00:00+00:00",
            )
            store.append_event(
                account_ref="acct_b",
                decision_code="blocked_suppression_dnc",
                counts_toward_attempt=False,
                recorded_at_utc="2026-02-08T11:00:00+00:00",
            )
            recent = store.list_recent_events(limit=10)
            self.assertEqual(len(recent), 2)
            self.assertEqual(recent[0]["decision_code"], "blocked_suppression_dnc")

    def test_set_and_get_suppressions_records_audit_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonContactAttemptStore(root_dir=tmpdir)
            self.assertEqual(
                store.get_suppressions("acct_sup"),
                {"dnc": False, "cease_contact": False, "legal_hold": False},
            )
            event = store.set_suppression(
                account_ref="acct_sup",
                flag="cease_contact",
                reason="in_call_stop_request",
                call_id="call_42",
            )
            self.assertEqual(event["decision_code"], "suppression_recorded_cease_contact")
            self.assertFalse(event["counts_toward_attempt"])
            self.assertEqual(event["reason"], "in_call_stop_request")
            self.assertEqual(event["call_id"], "call_42")

            suppressions = store.get_suppressions("acct_sup")
            self.assertTrue(suppressions["cease_contact"])
            self.assertFalse(suppressions["dnc"])
            self.assertFalse(suppressions["legal_hold"])

            events = store.list_events("acct_sup")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["decision_code"], "suppression_recorded_cease_contact")

    def test_set_suppression_rejects_invalid_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonContactAttemptStore(root_dir=tmpdir)
            with self.assertRaises(ValueError):
                store.set_suppression(account_ref="acct_bad", flag="unknown_flag", reason="test")

    def test_count_attempts_invalid_timezone_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonContactAttemptStore(root_dir=tmpdir)
            with self.assertRaises((ZoneInfoNotFoundError, ValueError)):
                store.count_attempts_for_local_day(
                    account_ref="acct_tz",
                    timezone_name="America/Chicagoo",
                    local_day_iso="2026-02-08",
                )


if __name__ == "__main__":
    unittest.main()

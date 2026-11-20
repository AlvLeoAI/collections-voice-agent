from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from src.api.compliance import evaluate_pre_dial_gate
from src.api.contact_attempt_store import JsonContactAttemptStore
from src.api.outbound_orchestration import CallPolicySnapshot


class ComplianceTests(unittest.TestCase):
    def _policy(self, *, daily_attempt_cap: int = 2, min_gap_minutes: int = 60) -> CallPolicySnapshot:
        return CallPolicySnapshot(
            timezone="America/Chicago",
            allowed_local_time_ranges=["08:00-20:00"],
            daily_attempt_cap=daily_attempt_cap,
            min_gap_minutes=min_gap_minutes,
        )

    def test_blocks_suppression_flags_non_retryable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_store = JsonContactAttemptStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 16, 0, tzinfo=UTC)  # 10:00 local
            decision = evaluate_pre_dial_gate(
                account_ref="acct_1",
                policy=self._policy(),
                suppression_flags={"dnc": True},
                attempt_store=attempt_store,
                now_utc=now,
            )
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason_code, "blocked_suppression_dnc")
            self.assertFalse(decision.retryable)

    def test_blocks_outside_call_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_store = JsonContactAttemptStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 9, 0, tzinfo=UTC)  # 03:00 local
            decision = evaluate_pre_dial_gate(
                account_ref="acct_2",
                policy=self._policy(),
                suppression_flags={},
                attempt_store=attempt_store,
                now_utc=now,
            )
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason_code, "blocked_policy_outside_call_window")
            self.assertTrue(decision.retryable)

    def test_blocks_daily_attempt_cap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_store = JsonContactAttemptStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 16, 0, tzinfo=UTC)  # 10:00 local
            for idx in range(2):
                attempt_store.append_event(
                    account_ref="acct_3",
                    decision_code="call_initialized",
                    counts_toward_attempt=True,
                    recorded_at_utc=(now - timedelta(hours=idx + 1)).isoformat(),
                )

            decision = evaluate_pre_dial_gate(
                account_ref="acct_3",
                policy=self._policy(daily_attempt_cap=2, min_gap_minutes=10),
                suppression_flags={},
                attempt_store=attempt_store,
                now_utc=now,
            )
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason_code, "blocked_policy_daily_attempt_cap")
            self.assertEqual(decision.attempts_today, 2)

    def test_blocks_min_gap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_store = JsonContactAttemptStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 16, 0, tzinfo=UTC)  # 10:00 local
            attempt_store.append_event(
                account_ref="acct_4",
                decision_code="call_initialized",
                counts_toward_attempt=True,
                recorded_at_utc=(now - timedelta(minutes=15)).isoformat(),
            )

            decision = evaluate_pre_dial_gate(
                account_ref="acct_4",
                policy=self._policy(daily_attempt_cap=5, min_gap_minutes=60),
                suppression_flags={},
                attempt_store=attempt_store,
                now_utc=now,
            )
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason_code, "blocked_policy_min_gap")
            self.assertIsNotNone(decision.min_gap_blocked_minutes_remaining)

    def test_allows_when_all_checks_pass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_store = JsonContactAttemptStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 16, 0, tzinfo=UTC)  # 10:00 local
            decision = evaluate_pre_dial_gate(
                account_ref="acct_5",
                policy=self._policy(),
                suppression_flags={"dnc": False, "cease_contact": False, "legal_hold": False},
                attempt_store=attempt_store,
                now_utc=now,
            )
            self.assertTrue(decision.allowed)
            self.assertEqual(decision.reason_code, "allowed")

    def test_blocks_invalid_timezone_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_store = JsonContactAttemptStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 9, 0, tzinfo=UTC)  # 03:00 America/Chicago
            policy = CallPolicySnapshot(
                timezone="America/Chicagoo",
                allowed_local_time_ranges=["08:00-20:00"],
                daily_attempt_cap=2,
                min_gap_minutes=60,
            )
            decision = evaluate_pre_dial_gate(
                account_ref="acct_tz",
                policy=policy,
                suppression_flags={},
                attempt_store=attempt_store,
                now_utc=now,
            )
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason_code, "blocked_policy_invalid_timezone")
            self.assertFalse(decision.retryable)

    def test_blocks_empty_call_window_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_store = JsonContactAttemptStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 16, 0, tzinfo=UTC)
            policy = CallPolicySnapshot(
                timezone="America/Chicago",
                allowed_local_time_ranges=[],
                daily_attempt_cap=2,
                min_gap_minutes=60,
            )
            decision = evaluate_pre_dial_gate(
                account_ref="acct_empty_win",
                policy=policy,
                suppression_flags={},
                attempt_store=attempt_store,
                now_utc=now,
            )
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason_code, "blocked_policy_missing_call_window")
            self.assertFalse(decision.retryable)

    def test_blocks_malformed_and_inverted_call_windows_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_store = JsonContactAttemptStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 16, 0, tzinfo=UTC)
            for bad_window in ("25:99-bad", "20:00-08:00", "10:00-10:00"):
                with self.subTest(bad_window=bad_window):
                    policy = CallPolicySnapshot(
                        timezone="America/Chicago",
                        allowed_local_time_ranges=[bad_window],
                        daily_attempt_cap=2,
                        min_gap_minutes=60,
                    )
                    decision = evaluate_pre_dial_gate(
                        account_ref="acct_bad_win",
                        policy=policy,
                        suppression_flags={},
                        attempt_store=attempt_store,
                        now_utc=now,
                    )
                    self.assertFalse(decision.allowed)
                    self.assertEqual(decision.reason_code, "blocked_policy_invalid_call_window")
                    self.assertFalse(decision.retryable)

    def test_blocks_invalid_cap_or_gap_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_store = JsonContactAttemptStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 16, 0, tzinfo=UTC)
            for cap, gap in ((0, 60), (2, -1)):
                with self.subTest(cap=cap, gap=gap):
                    decision = evaluate_pre_dial_gate(
                        account_ref="acct_bad_cfg",
                        policy=self._policy(daily_attempt_cap=cap, min_gap_minutes=gap),
                        suppression_flags={},
                        attempt_store=attempt_store,
                        now_utc=now,
                    )
                    self.assertFalse(decision.allowed)
                    self.assertEqual(decision.reason_code, "blocked_policy_invalid_config")
                    self.assertFalse(decision.retryable)

    def test_persisted_cease_contact_overrides_job_flags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_store = JsonContactAttemptStore(root_dir=tmpdir)
            attempt_store.set_suppression(
                account_ref="acct_cease",
                flag="cease_contact",
                reason="in_call_stop_request",
                call_id="call_123",
            )
            now = datetime(2026, 2, 8, 16, 0, tzinfo=UTC)
            decision = evaluate_pre_dial_gate(
                account_ref="acct_cease",
                policy=self._policy(),
                suppression_flags={"dnc": False, "cease_contact": False, "legal_hold": False},
                attempt_store=attempt_store,
                now_utc=now,
            )
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason_code, "blocked_suppression_cease_contact")
            self.assertFalse(decision.retryable)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from src.api.job_store import JsonJobStore
from src.api.outbound_orchestration import (
    CallPolicySnapshot,
    JobState,
    OutboundCallPayload,
    TriggerSource,
)


class JobStoreTests(unittest.TestCase):
    def _payload(self) -> OutboundCallPayload:
        return OutboundCallPayload(
            account_ref="acct_2001",
            party_profile={"target_name": "Alex Morgan", "callback_number": "+1 (555) 010-2000"},
            account_context_ref="ctx_CASE_2001",
            language="en-US",
        )

    def _policy(self) -> CallPolicySnapshot:
        return CallPolicySnapshot(
            timezone="America/Chicago",
            allowed_local_time_ranges=["08:00-20:00"],
            daily_attempt_cap=2,
            min_gap_minutes=60,
        )

    def test_enqueue_is_idempotent_for_same_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonJobStore(root_dir=tmpdir)
            scheduled = "2026-02-10T15:00:00+00:00"

            first, created_1 = store.enqueue_job(
                trigger_source=TriggerSource.CRON,
                campaign_id="cmp_1",
                payload=self._payload(),
                policy=self._policy(),
                scheduled_for_utc=scheduled,
            )
            second, created_2 = store.enqueue_job(
                trigger_source=TriggerSource.WEBHOOK,
                campaign_id="cmp_1",
                payload=self._payload(),
                policy=self._policy(),
                scheduled_for_utc=scheduled,
            )

            self.assertTrue(created_1)
            self.assertFalse(created_2)
            self.assertEqual(first.job_id, second.job_id)
            self.assertEqual(len(store.list_jobs()), 1)

    def test_lease_start_and_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonJobStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 12, 0, tzinfo=UTC)
            scheduled = (now - timedelta(minutes=1)).isoformat()

            job, _ = store.enqueue_job(
                trigger_source=TriggerSource.MANUAL,
                campaign_id="cmp_2",
                payload=self._payload(),
                policy=self._policy(),
                scheduled_for_utc=scheduled,
            )

            leased = store.lease_next_due_job(worker_id="worker_t", now_utc=now)
            self.assertIsNotNone(leased)
            self.assertEqual(leased.job_id, job.job_id)
            self.assertEqual(leased.state, JobState.LEASED)

            started = store.mark_job_started(job.job_id, now_utc=now + timedelta(seconds=5))
            self.assertEqual(started.state, JobState.RUNNING)
            self.assertEqual(len(started.attempts), 1)

            done = store.mark_job_succeeded(
                job.job_id,
                outcome_code="call_initialized",
                call_id="call_abc",
                now_utc=now + timedelta(seconds=30),
            )
            self.assertEqual(done.state, JobState.SUCCEEDED)
            self.assertEqual(done.attempts[-1].call_id, "call_abc")

    def test_failed_job_requeues_when_due(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonJobStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 12, 0, tzinfo=UTC)
            scheduled = (now - timedelta(minutes=1)).isoformat()

            job, _ = store.enqueue_job(
                trigger_source=TriggerSource.CRON,
                campaign_id="cmp_3",
                payload=self._payload(),
                policy=self._policy(),
                scheduled_for_utc=scheduled,
            )
            store.lease_next_due_job(worker_id="worker_t", now_utc=now)
            store.mark_job_started(job.job_id, now_utc=now + timedelta(seconds=1))
            failed = store.mark_job_failed(
                job.job_id,
                error_code="dial_timeout",
                now_utc=now + timedelta(seconds=2),
            )
            self.assertEqual(failed.state, JobState.WAITING_RETRY)
            self.assertIsNotNone(failed.next_attempt_at_utc)

            due = datetime.fromisoformat(failed.next_attempt_at_utc.replace("Z", "+00:00"))
            changed = store.requeue_due_retries(now_utc=due)
            self.assertEqual(changed, 1)
            refreshed = store.get_job(job.job_id)
            self.assertEqual(refreshed.state, JobState.QUEUED)

    def test_defer_from_lease_does_not_create_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonJobStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 12, 0, tzinfo=UTC)
            scheduled = (now - timedelta(minutes=1)).isoformat()

            job, _ = store.enqueue_job(
                trigger_source=TriggerSource.MANUAL,
                campaign_id="cmp_4",
                payload=self._payload(),
                policy=self._policy(),
                scheduled_for_utc=scheduled,
            )
            store.lease_next_due_job(worker_id="worker_t", now_utc=now)
            deferred = store.defer_leased_job(
                job.job_id,
                error_code="blocked_policy_min_gap",
                delay_seconds=300,
                now_utc=now,
            )
            self.assertEqual(deferred.state, JobState.WAITING_RETRY)
            self.assertEqual(len(deferred.attempts), 0)
            self.assertEqual(deferred.failure_reason, "blocked_policy_min_gap")

    def test_cancel_job_sets_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonJobStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 12, 0, tzinfo=UTC)
            scheduled = (now - timedelta(minutes=1)).isoformat()

            job, _ = store.enqueue_job(
                trigger_source=TriggerSource.MANUAL,
                campaign_id="cmp_5",
                payload=self._payload(),
                policy=self._policy(),
                scheduled_for_utc=scheduled,
            )
            store.lease_next_due_job(worker_id="worker_t", now_utc=now)
            canceled = store.cancel_job(job.job_id, reason_code="blocked_suppression_dnc")
            self.assertEqual(canceled.state, JobState.CANCELED)
            self.assertEqual(canceled.failure_reason, "blocked_suppression_dnc")

    def test_lease_next_due_job_reclaims_expired_lease_for_second_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonJobStore(root_dir=tmpdir)
            now = datetime(2026, 2, 8, 12, 0, tzinfo=UTC)
            scheduled = (now - timedelta(minutes=1)).isoformat()

            job, _ = store.enqueue_job(
                trigger_source=TriggerSource.MANUAL,
                campaign_id="cmp_6",
                payload=self._payload(),
                policy=self._policy(),
                scheduled_for_utc=scheduled,
            )
            leased_1 = store.lease_next_due_job(worker_id="worker_1", lease_seconds=30, now_utc=now)
            self.assertIsNotNone(leased_1)
            self.assertEqual(leased_1.lease_owner, "worker_1")

            # Still leased before expiry
            self.assertIsNone(
                store.lease_next_due_job(worker_id="worker_2", lease_seconds=30, now_utc=now + timedelta(seconds=10))
            )

            # Reclaimed automatically once expired
            leased_2 = store.lease_next_due_job(
                worker_id="worker_2", lease_seconds=30, now_utc=now + timedelta(seconds=35)
            )
            self.assertIsNotNone(leased_2)
            self.assertEqual(leased_2.job_id, job.job_id)
            self.assertEqual(leased_2.lease_owner, "worker_2")

    def test_enqueue_deduplicates_without_scheduled_for_utc_and_supports_custom_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonJobStore(root_dir=tmpdir)
            j1, created_1 = store.enqueue_job(
                trigger_source=TriggerSource.MANUAL,
                campaign_id="cmp_7",
                payload=self._payload(),
                policy=self._policy(),
            )
            j2, created_2 = store.enqueue_job(
                trigger_source=TriggerSource.WEBHOOK,
                campaign_id="cmp_7",
                payload=self._payload(),
                policy=self._policy(),
            )
            self.assertTrue(created_1)
            self.assertFalse(created_2)
            self.assertEqual(j1.job_id, j2.job_id)

            j3, created_3 = store.enqueue_job(
                trigger_source=TriggerSource.MANUAL,
                campaign_id="cmp_7",
                payload=self._payload(),
                policy=self._policy(),
                idempotency_key="caller_key_1",
            )
            j4, created_4 = store.enqueue_job(
                trigger_source=TriggerSource.MANUAL,
                campaign_id="cmp_7",
                payload=self._payload(),
                policy=self._policy(),
                idempotency_key="caller_key_1",
            )
            self.assertTrue(created_3)
            self.assertFalse(created_4)
            self.assertEqual(j3.job_id, j4.job_id)
            self.assertNotEqual(j1.job_id, j3.job_id)


if __name__ == "__main__":
    unittest.main()

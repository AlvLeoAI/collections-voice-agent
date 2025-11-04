from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.api.outbound_orchestration import (
    CallPolicySnapshot,
    JobEvent,
    JobState,
    OutboundCallPayload,
    TriggerSource,
    compute_retry_delay_seconds,
    create_job,
    lease_job,
    move_retry_to_queue,
    start_job_attempt,
    transition_state,
)


class OutboundOrchestrationTests(unittest.TestCase):
    def _sample_payload(self) -> OutboundCallPayload:
        return OutboundCallPayload(
            account_ref="acct_1001",
            party_profile={"target_name": "Alex Morgan", "callback_number": "+1 (555) 010-2000"},
            account_context_ref="ctx_CASE_DEMO_001",
            language="en-US",
        )

    def _sample_policy(self) -> CallPolicySnapshot:
        return CallPolicySnapshot(
            timezone="America/Chicago",
            allowed_local_time_ranges=["08:00-20:00"],
            daily_attempt_cap=2,
            min_gap_minutes=60,
        )

    def test_happy_path_state_machine(self):
        job = create_job(
            job_id="job_001",
            trigger_source=TriggerSource.CRON,
            campaign_id="cmp_feb_2026",
            payload=self._sample_payload(),
            policy=self._sample_policy(),
        )
        self.assertEqual(job.state, JobState.QUEUED)

        lease_job(job, worker_id="worker_a", lease_seconds=60)
        self.assertEqual(job.state, JobState.LEASED)
        self.assertEqual(job.lease_owner, "worker_a")

        start_job_attempt(job)
        self.assertEqual(job.state, JobState.RUNNING)
        self.assertEqual(len(job.attempts), 1)

        now = datetime.now(timezone.utc)
        job.mark_succeeded(now_utc=now, outcome_code="ptp_set")
        self.assertEqual(job.state, JobState.SUCCEEDED)
        self.assertEqual(job.attempts[0].outcome_code, "ptp_set")

    def test_failure_then_retry_then_queue(self):
        now = datetime(2026, 2, 8, 12, 0, tzinfo=timezone.utc)
        job = create_job(
            job_id="job_002",
            trigger_source=TriggerSource.WEBHOOK,
            campaign_id="cmp_feb_2026",
            payload=self._sample_payload(),
            policy=self._sample_policy(),
        )
        lease_job(job, worker_id="worker_b", now_utc=now)
        start_job_attempt(job, now_utc=now + timedelta(seconds=5))

        job.mark_failed_and_schedule_retry(
            error_code="dial_timeout",
            now_utc=now + timedelta(seconds=15),
        )
        self.assertEqual(job.state, JobState.WAITING_RETRY)
        self.assertIsNotNone(job.next_attempt_at_utc)

        due = datetime.fromisoformat(job.next_attempt_at_utc.replace("Z", "+00:00"))
        move_retry_to_queue(job, now_utc=due)
        self.assertEqual(job.state, JobState.QUEUED)

    def test_exhaust_retries_dead_letters_job(self):
        now = datetime(2026, 2, 8, 12, 0, tzinfo=timezone.utc)
        job = create_job(
            job_id="job_003",
            trigger_source=TriggerSource.CRON,
            campaign_id="cmp_feb_2026",
            payload=self._sample_payload(),
            policy=self._sample_policy(),
        )

        for idx in range(3):
            lease_job(job, worker_id=f"worker_{idx}", now_utc=now + timedelta(minutes=idx))
            start_job_attempt(job, now_utc=now + timedelta(minutes=idx, seconds=5))
            job.mark_failed_and_schedule_retry(
                error_code="carrier_reject",
                now_utc=now + timedelta(minutes=idx, seconds=10),
            )
            if idx < 2:
                due = datetime.fromisoformat(job.next_attempt_at_utc.replace("Z", "+00:00"))
                move_retry_to_queue(job, now_utc=due)

        self.assertEqual(job.state, JobState.DEAD_LETTER)
        self.assertEqual(job.failure_reason, "carrier_reject")

    def test_backoff_is_exponential_and_capped(self):
        self.assertEqual(compute_retry_delay_seconds(attempt_number=1, base_delay_seconds=120), 120)
        self.assertEqual(compute_retry_delay_seconds(attempt_number=2, base_delay_seconds=120), 240)
        self.assertEqual(compute_retry_delay_seconds(attempt_number=3, base_delay_seconds=120), 480)
        self.assertEqual(
            compute_retry_delay_seconds(
                attempt_number=10,
                base_delay_seconds=120,
                max_delay_seconds=600,
            ),
            600,
        )

    def test_state_machine_allows_leased_to_waiting_retry(self):
        self.assertEqual(
            transition_state(JobState.LEASED, JobEvent.SCHEDULE_RETRY),
            JobState.WAITING_RETRY,
        )


if __name__ == "__main__":
    unittest.main()

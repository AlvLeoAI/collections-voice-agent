from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from src.api.outbound_orchestration import (
    CallPolicySnapshot,
    JobEvent,
    JobAttempt,
    JobState,
    OutboundCallJob,
    OutboundCallPayload,
    RetryPolicy,
    TriggerSource,
    create_job,
    lease_job,
    move_retry_to_queue,
    parse_iso_utc,
    start_job_attempt,
    to_iso_utc,
    transition_state,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _job_to_row(job: OutboundCallJob) -> Dict[str, Any]:
    row = asdict(job)
    row["trigger_source"] = job.trigger_source.value
    row["state"] = job.state.value
    return row


def _job_from_row(row: Dict[str, Any]) -> OutboundCallJob:
    payload_data = row.get("payload") or {}
    policy_data = row.get("policy") or {}
    retry_data = row.get("retry_policy") or {}

    attempts_data = row.get("attempts", []) or []
    attempts: List[JobAttempt] = []
    for item in attempts_data:
        if not isinstance(item, dict):
            continue
        attempts.append(
            JobAttempt(
                attempt_number=int(item.get("attempt_number", len(attempts) + 1)),
                started_at_utc=str(item.get("started_at_utc", "")),
                ended_at_utc=item.get("ended_at_utc"),
                outcome_code=item.get("outcome_code"),
                error_code=item.get("error_code"),
                call_id=item.get("call_id"),
            )
        )

    return OutboundCallJob(
        job_id=str(row["job_id"]),
        idempotency_key=str(row["idempotency_key"]),
        trigger_source=TriggerSource(str(row["trigger_source"])),
        campaign_id=str(row["campaign_id"]),
        priority=int(row.get("priority", 100)),
        created_at_utc=str(row["created_at_utc"]),
        scheduled_for_utc=str(row["scheduled_for_utc"]),
        state=JobState(str(row["state"])),
        payload=OutboundCallPayload(
            account_ref=str(payload_data.get("account_ref", "")),
            party_profile=dict(payload_data.get("party_profile", {})),
            account_context_ref=str(payload_data.get("account_context_ref", "")),
            language=str(payload_data.get("language", "en-US")),
            suppression_flags=dict(payload_data.get("suppression_flags", {})),
        ),
        policy=CallPolicySnapshot(
            timezone=str(policy_data.get("timezone", "America/Chicago")),
            allowed_local_time_ranges=list(policy_data.get("allowed_local_time_ranges", [])),
            daily_attempt_cap=int(policy_data.get("daily_attempt_cap", 2)),
            min_gap_minutes=int(policy_data.get("min_gap_minutes", 60)),
        ),
        retry_policy=RetryPolicy(
            max_attempts=int(retry_data.get("max_attempts", 3)),
            base_delay_seconds=int(retry_data.get("base_delay_seconds", 120)),
            max_delay_seconds=int(retry_data.get("max_delay_seconds", 3600)),
        ),
        lease_owner=row.get("lease_owner"),
        lease_expires_at_utc=row.get("lease_expires_at_utc"),
        next_attempt_at_utc=row.get("next_attempt_at_utc"),
        attempts=attempts,
        failure_reason=row.get("failure_reason"),
    )


def _is_due(job: OutboundCallJob, *, now_utc: datetime) -> bool:
    due_at = job.next_attempt_at_utc or job.scheduled_for_utc
    return parse_iso_utc(due_at) <= now_utc


class JsonJobStore:
    """Persist outbound call jobs in local JSON files under runtime/jobs."""

    def __init__(self, root_dir: str | Path = "runtime/jobs") -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def generate_job_id(self) -> str:
        return f"job_{uuid.uuid4().hex}"

    def _path_for(self, job_id: str) -> Path:
        return self.root_dir / f"{job_id}.json"

    def enqueue_job(
        self,
        *,
        trigger_source: TriggerSource,
        campaign_id: str,
        payload: OutboundCallPayload,
        policy: CallPolicySnapshot,
        scheduled_for_utc: Optional[str] = None,
        priority: int = 100,
        retry_policy: Optional[RetryPolicy] = None,
    ) -> Tuple[OutboundCallJob, bool]:
        with self._lock:
            new_job = create_job(
                job_id=self.generate_job_id(),
                trigger_source=trigger_source,
                campaign_id=campaign_id,
                payload=payload,
                policy=policy,
                scheduled_for_utc=scheduled_for_utc,
                priority=priority,
                retry_policy=retry_policy,
            )
            existing = self._find_by_idempotency_locked(new_job.idempotency_key)
            if existing is not None:
                return existing, False
            self._write_locked(new_job)
            return new_job, True

    def get_job(self, job_id: str) -> OutboundCallJob:
        with self._lock:
            path = self._path_for(job_id)
            if not path.exists():
                raise FileNotFoundError(f"Unknown job_id: {job_id}")
            with open(path, "r", encoding="utf-8") as f:
                return _job_from_row(json.load(f))

    def list_jobs(
        self,
        *,
        state: Optional[JobState] = None,
        campaign_id: Optional[str] = None,
    ) -> List[OutboundCallJob]:
        with self._lock:
            jobs = self._load_all_locked()
            if state is not None:
                jobs = [j for j in jobs if j.state == state]
            if campaign_id:
                jobs = [j for j in jobs if j.campaign_id == campaign_id]
            jobs.sort(key=lambda j: (j.priority, j.created_at_utc))
            return jobs

    def requeue_due_retries(self, *, now_utc: Optional[datetime] = None) -> int:
        now = now_utc or _utc_now()
        changed = 0
        with self._lock:
            jobs = self._load_all_locked()
            for job in jobs:
                if job.state != JobState.WAITING_RETRY:
                    continue
                try:
                    move_retry_to_queue(job, now_utc=now)
                except ValueError:
                    continue
                self._write_locked(job)
                changed += 1
        return changed

    def lease_next_due_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 90,
        now_utc: Optional[datetime] = None,
    ) -> Optional[OutboundCallJob]:
        now = now_utc or _utc_now()
        with self._lock:
            jobs = self._load_all_locked()

            # First, auto-requeue any waiting retries that are now due.
            for job in jobs:
                if job.state == JobState.WAITING_RETRY:
                    try:
                        move_retry_to_queue(job, now_utc=now)
                    except ValueError:
                        continue
                    self._write_locked(job)

            candidates = [
                j
                for j in jobs
                if j.state == JobState.QUEUED and _is_due(j, now_utc=now)
            ]
            if not candidates:
                return None

            candidates.sort(key=lambda j: (j.priority, j.created_at_utc))
            selected = candidates[0]
            lease_job(selected, worker_id=worker_id, lease_seconds=lease_seconds, now_utc=now)
            self._write_locked(selected)
            return selected

    def mark_job_started(self, job_id: str, *, now_utc: Optional[datetime] = None) -> OutboundCallJob:
        now = now_utc or _utc_now()
        with self._lock:
            job = self._read_locked(job_id)
            start_job_attempt(job, now_utc=now)
            self._write_locked(job)
            return job

    def defer_leased_job(
        self,
        job_id: str,
        *,
        error_code: str,
        delay_seconds: int,
        now_utc: Optional[datetime] = None,
    ) -> OutboundCallJob:
        now = now_utc or _utc_now()
        with self._lock:
            job = self._read_locked(job_id)
            if job.state != JobState.LEASED:
                raise ValueError(f"Job must be leased before deferring. state={job.state.value}")
            job.state = transition_state(job.state, JobEvent.SCHEDULE_RETRY)
            job.next_attempt_at_utc = to_iso_utc(now + timedelta(seconds=max(1, delay_seconds)))
            job.failure_reason = error_code
            job.lease_owner = None
            job.lease_expires_at_utc = None
            self._write_locked(job)
            return job

    def cancel_job(
        self,
        job_id: str,
        *,
        reason_code: str,
        now_utc: Optional[datetime] = None,
    ) -> OutboundCallJob:
        _ = now_utc or _utc_now()
        with self._lock:
            job = self._read_locked(job_id)
            job.state = transition_state(job.state, JobEvent.CANCEL)
            job.failure_reason = reason_code
            job.lease_owner = None
            job.lease_expires_at_utc = None
            self._write_locked(job)
            return job

    def mark_job_succeeded(
        self,
        job_id: str,
        *,
        outcome_code: str,
        call_id: Optional[str] = None,
        now_utc: Optional[datetime] = None,
    ) -> OutboundCallJob:
        now = now_utc or _utc_now()
        with self._lock:
            job = self._read_locked(job_id)
            if job.attempts and call_id:
                job.attempts[-1].call_id = call_id
            job.mark_succeeded(now_utc=now, outcome_code=outcome_code)
            self._write_locked(job)
            return job

    def mark_job_failed(
        self,
        job_id: str,
        *,
        error_code: str,
        call_id: Optional[str] = None,
        now_utc: Optional[datetime] = None,
    ) -> OutboundCallJob:
        now = now_utc or _utc_now()
        with self._lock:
            job = self._read_locked(job_id)
            if job.attempts and call_id:
                job.attempts[-1].call_id = call_id
            job.mark_failed_and_schedule_retry(error_code=error_code, now_utc=now)
            self._write_locked(job)
            return job

    def _find_by_idempotency_locked(self, idempotency_key: str) -> Optional[OutboundCallJob]:
        for job in self._load_all_locked():
            if job.idempotency_key == idempotency_key:
                return job
        return None

    def _load_all_locked(self) -> List[OutboundCallJob]:
        jobs: List[OutboundCallJob] = []
        for path in sorted(self.root_dir.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    row = json.load(f)
                jobs.append(_job_from_row(row))
            except Exception:
                continue
        return jobs

    def _read_locked(self, job_id: str) -> OutboundCallJob:
        path = self._path_for(job_id)
        if not path.exists():
            raise FileNotFoundError(f"Unknown job_id: {job_id}")
        with open(path, "r", encoding="utf-8") as f:
            return _job_from_row(json.load(f))

    def _write_locked(self, job: OutboundCallJob) -> None:
        path = self._path_for(job.job_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_job_to_row(job), f, indent=2)

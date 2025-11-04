from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class TriggerSource(str, Enum):
    CRON = "cron"
    WEBHOOK = "webhook"
    MANUAL = "manual"


class JobState(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    WAITING_RETRY = "waiting_retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELED = "canceled"


class JobEvent(str, Enum):
    LEASE = "lease"
    START = "start"
    CALL_SUCCEEDED = "call_succeeded"
    CALL_FAILED = "call_failed"
    SCHEDULE_RETRY = "schedule_retry"
    RETRY_READY = "retry_ready"
    EXHAUST_RETRIES = "exhaust_retries"
    CANCEL = "cancel"


# Formal worker state machine.
STATE_TRANSITIONS: Dict[Tuple[JobState, JobEvent], JobState] = {
    (JobState.QUEUED, JobEvent.LEASE): JobState.LEASED,
    (JobState.LEASED, JobEvent.START): JobState.RUNNING,
    (JobState.RUNNING, JobEvent.CALL_SUCCEEDED): JobState.SUCCEEDED,
    (JobState.RUNNING, JobEvent.CALL_FAILED): JobState.FAILED,
    (JobState.LEASED, JobEvent.SCHEDULE_RETRY): JobState.WAITING_RETRY,
    (JobState.FAILED, JobEvent.SCHEDULE_RETRY): JobState.WAITING_RETRY,
    (JobState.WAITING_RETRY, JobEvent.RETRY_READY): JobState.QUEUED,
    (JobState.FAILED, JobEvent.EXHAUST_RETRIES): JobState.DEAD_LETTER,
    (JobState.QUEUED, JobEvent.CANCEL): JobState.CANCELED,
    (JobState.LEASED, JobEvent.CANCEL): JobState.CANCELED,
    (JobState.RUNNING, JobEvent.CANCEL): JobState.CANCELED,
    (JobState.WAITING_RETRY, JobEvent.CANCEL): JobState.CANCELED,
}


def transition_state(current: JobState, event: JobEvent) -> JobState:
    key = (current, event)
    if key not in STATE_TRANSITIONS:
        raise ValueError(f"Invalid transition: state={current.value}, event={event.value}")
    return STATE_TRANSITIONS[key]


def is_terminal_state(state: JobState) -> bool:
    return state in {
        JobState.SUCCEEDED,
        JobState.DEAD_LETTER,
        JobState.CANCELED,
    }


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: int = 120
    max_delay_seconds: int = 3600


@dataclass(frozen=True)
class CallPolicySnapshot:
    timezone: str
    # 24h windows in local time. Example: ["08:00-20:00"].
    allowed_local_time_ranges: List[str]
    daily_attempt_cap: int = 2
    min_gap_minutes: int = 60


@dataclass(frozen=True)
class OutboundCallPayload:
    account_ref: str
    party_profile: Dict[str, str]
    account_context_ref: str
    language: str = "en-US"
    suppression_flags: Dict[str, bool] = field(default_factory=dict)


@dataclass
class JobAttempt:
    attempt_number: int
    started_at_utc: str
    ended_at_utc: Optional[str] = None
    outcome_code: Optional[str] = None
    error_code: Optional[str] = None
    call_id: Optional[str] = None


@dataclass
class OutboundCallJob:
    job_id: str
    idempotency_key: str
    trigger_source: TriggerSource
    campaign_id: str
    priority: int
    created_at_utc: str
    scheduled_for_utc: str
    state: JobState
    payload: OutboundCallPayload
    policy: CallPolicySnapshot
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    lease_owner: Optional[str] = None
    lease_expires_at_utc: Optional[str] = None
    next_attempt_at_utc: Optional[str] = None
    attempts: List[JobAttempt] = field(default_factory=list)
    failure_reason: Optional[str] = None

    def can_attempt_again(self) -> bool:
        return len(self.attempts) < self.retry_policy.max_attempts

    def mark_failed_and_schedule_retry(self, *, error_code: str, now_utc: datetime) -> None:
        if not self.attempts:
            raise ValueError("Cannot schedule retry without an attempt record.")
        self.attempts[-1].ended_at_utc = to_iso_utc(now_utc)
        self.attempts[-1].error_code = error_code
        self.state = transition_state(self.state, JobEvent.CALL_FAILED)

        if not self.can_attempt_again():
            self.state = transition_state(self.state, JobEvent.EXHAUST_RETRIES)
            self.failure_reason = error_code
            return

        self.state = transition_state(self.state, JobEvent.SCHEDULE_RETRY)
        delay_seconds = compute_retry_delay_seconds(
            attempt_number=len(self.attempts),
            base_delay_seconds=self.retry_policy.base_delay_seconds,
            max_delay_seconds=self.retry_policy.max_delay_seconds,
        )
        self.next_attempt_at_utc = to_iso_utc(now_utc + timedelta(seconds=delay_seconds))
        self.lease_owner = None
        self.lease_expires_at_utc = None

    def mark_succeeded(self, *, now_utc: datetime, outcome_code: str) -> None:
        if not self.attempts:
            raise ValueError("Cannot mark success without an attempt record.")
        self.attempts[-1].ended_at_utc = to_iso_utc(now_utc)
        self.attempts[-1].outcome_code = outcome_code
        self.state = transition_state(self.state, JobEvent.CALL_SUCCEEDED)
        self.lease_owner = None
        self.lease_expires_at_utc = None
        self.next_attempt_at_utc = None


def compute_retry_delay_seconds(
    *,
    attempt_number: int,
    base_delay_seconds: int = 120,
    max_delay_seconds: int = 3600,
) -> int:
    # Exponential backoff without randomness for deterministic demo behavior.
    raw_delay = base_delay_seconds * (2 ** max(0, attempt_number - 1))
    return min(raw_delay, max_delay_seconds)


def build_idempotency_key(
    *,
    campaign_id: str,
    account_ref: str,
    scheduled_for_utc: str,
) -> str:
    # Keep the stable key free of raw PII and usable across trigger types.
    digest = hashlib.sha256(
        f"{campaign_id}|{account_ref}|{scheduled_for_utc}".encode("utf-8")
    ).hexdigest()
    return f"job_{digest[:24]}"


def create_job(
    *,
    job_id: str,
    trigger_source: TriggerSource,
    campaign_id: str,
    payload: OutboundCallPayload,
    policy: CallPolicySnapshot,
    scheduled_for_utc: Optional[str] = None,
    priority: int = 100,
    retry_policy: Optional[RetryPolicy] = None,
) -> OutboundCallJob:
    now = utc_now()
    scheduled = scheduled_for_utc or to_iso_utc(now)
    idempotency_key = build_idempotency_key(
        campaign_id=campaign_id,
        account_ref=payload.account_ref,
        scheduled_for_utc=scheduled,
    )
    return OutboundCallJob(
        job_id=job_id,
        idempotency_key=idempotency_key,
        trigger_source=trigger_source,
        campaign_id=campaign_id,
        priority=priority,
        created_at_utc=to_iso_utc(now),
        scheduled_for_utc=scheduled,
        state=JobState.QUEUED,
        payload=payload,
        policy=policy,
        retry_policy=retry_policy or RetryPolicy(),
        next_attempt_at_utc=scheduled,
    )


def lease_job(
    job: OutboundCallJob,
    *,
    worker_id: str,
    lease_seconds: int = 90,
    now_utc: Optional[datetime] = None,
) -> None:
    now = now_utc or utc_now()
    if job.state != JobState.QUEUED:
        raise ValueError(f"Job must be queued before leasing. state={job.state.value}")
    job.state = transition_state(job.state, JobEvent.LEASE)
    job.lease_owner = worker_id
    job.lease_expires_at_utc = to_iso_utc(now + timedelta(seconds=lease_seconds))


def start_job_attempt(job: OutboundCallJob, *, now_utc: Optional[datetime] = None) -> None:
    now = now_utc or utc_now()
    if job.state != JobState.LEASED:
        raise ValueError(f"Job must be leased before start. state={job.state.value}")
    job.state = transition_state(job.state, JobEvent.START)
    job.attempts.append(
        JobAttempt(
            attempt_number=len(job.attempts) + 1,
            started_at_utc=to_iso_utc(now),
        )
    )


def move_retry_to_queue(job: OutboundCallJob, *, now_utc: Optional[datetime] = None) -> None:
    now = now_utc or utc_now()
    if job.state != JobState.WAITING_RETRY:
        raise ValueError(
            f"Job must be waiting_retry before retry-ready transition. state={job.state.value}"
        )
    if not job.next_attempt_at_utc:
        raise ValueError("next_attempt_at_utc is required for retry jobs.")
    due = parse_iso_utc(job.next_attempt_at_utc)
    if now < due:
        raise ValueError("Retry window is not due yet.")
    job.state = transition_state(job.state, JobEvent.RETRY_READY)
    job.next_attempt_at_utc = to_iso_utc(now)

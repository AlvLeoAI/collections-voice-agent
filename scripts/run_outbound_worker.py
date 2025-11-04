#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure project root is on sys.path when running from ./scripts
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.api.call_store import JsonCallStore
from src.api.compliance import evaluate_pre_dial_gate
from src.api.contact_attempt_store import JsonContactAttemptStore
from src.api.job_store import JsonJobStore
from src.api.outbound_orchestration import JobState
from src.outbound_voice_agent import CallState, start_call


def _process_one_job(
    job_store: JsonJobStore,
    call_store: JsonCallStore,
    attempt_store: JsonContactAttemptStore,
    *,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    leased = job_store.lease_next_due_job(worker_id=worker_id, lease_seconds=lease_seconds)
    if leased is None:
        return False

    job_id = leased.job_id
    try:
        now_utc = datetime.now(timezone.utc)
        decision = evaluate_pre_dial_gate(
            account_ref=leased.payload.account_ref,
            policy=leased.policy,
            suppression_flags=leased.payload.suppression_flags,
            attempt_store=attempt_store,
            now_utc=now_utc,
        )
        if not decision.allowed:
            attempt_store.append_event(
                account_ref=leased.payload.account_ref,
                decision_code=decision.reason_code,
                counts_toward_attempt=False,
                job_id=job_id,
            )
            if decision.retryable:
                delay_seconds = decision.retry_after_seconds or 900
                job_store.defer_leased_job(
                    job_id,
                    error_code=decision.reason_code,
                    delay_seconds=delay_seconds,
                    now_utc=now_utc,
                )
                print(f"[retry] job_id={job_id} error={decision.reason_code} retry_in_s={delay_seconds}")
            else:
                job_store.cancel_job(job_id, reason_code=decision.reason_code, now_utc=now_utc)
                print(f"[blocked] job_id={job_id} outcome={decision.reason_code}")
            return True

        started = job_store.mark_job_started(job_id, now_utc=now_utc)
        result = start_call(call_state=CallState(), party_profile=started.payload.party_profile)
        call_id = call_store.generate_call_id()
        call_store.create_call(
            call_id=call_id,
            assistant_intent=result["assistant_intent"],
            call_state=result["call_state"],
        )
        job_store.mark_job_succeeded(job_id, outcome_code="call_initialized", call_id=call_id)
        attempt_store.append_event(
            account_ref=started.payload.account_ref,
            decision_code="call_initialized",
            counts_toward_attempt=True,
            job_id=job_id,
            call_id=call_id,
        )
        print(f"[ok] job_id={job_id} call_id={call_id} outcome=call_initialized")
        return True
    except Exception as exc:
        error_code = f"worker_exception:{exc.__class__.__name__}"
        try:
            current = job_store.get_job(job_id)
            if current.state == JobState.RUNNING:
                job_store.mark_job_failed(job_id, error_code=error_code)
            elif current.state == JobState.LEASED:
                job_store.defer_leased_job(job_id, error_code=error_code, delay_seconds=120)
        except Exception:
            pass
        try:
            account_ref: Optional[str] = None
            try:
                account_ref = leased.payload.account_ref
            except Exception:
                account_ref = None
            if account_ref:
                attempt_store.append_event(
                    account_ref=account_ref,
                    decision_code=error_code,
                    counts_toward_attempt=False,
                    job_id=job_id,
                )
        except Exception:
            pass
        print(f"[error] job_id={job_id} error={error_code}")
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run outbound queue worker for call initialization.")
    parser.add_argument("--worker-id", type=str, default="worker_local")
    parser.add_argument("--lease-seconds", type=int, default=90)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--max-jobs", type=int, default=0, help="0 means no fixed limit.")
    parser.add_argument("--once", action="store_true", help="Run at most one lease/attempt cycle.")
    parser.add_argument("--jobs-dir", type=str, default="runtime/jobs")
    parser.add_argument("--calls-dir", type=str, default="runtime/calls")
    parser.add_argument("--attempts-dir", type=str, default="runtime/attempts")
    args = parser.parse_args()

    job_store = JsonJobStore(root_dir=args.jobs_dir)
    call_store = JsonCallStore(root_dir=args.calls_dir)
    attempt_store = JsonContactAttemptStore(root_dir=args.attempts_dir)

    processed = 0
    while True:
        did_work = _process_one_job(
            job_store,
            call_store,
            attempt_store,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
        )

        if did_work:
            processed += 1
            if args.once:
                break
            if args.max_jobs > 0 and processed >= args.max_jobs:
                break
            continue

        if args.once:
            print("[idle] no_due_jobs")
            break

        if args.max_jobs > 0 and processed >= args.max_jobs:
            break

        time.sleep(max(0.1, args.poll_seconds))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

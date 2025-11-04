#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import List, Tuple

# Ensure project root is on sys.path when running from ./scripts
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.api.contact_attempt_store import JsonContactAttemptStore
from src.api.job_store import JsonJobStore
from src.api.metrics import build_job_metrics_summary
from src.api.outbound_orchestration import CallPolicySnapshot, OutboundCallPayload, TriggerSource


def _run_worker_once(
    *,
    jobs_dir: str,
    calls_dir: str,
    attempts_dir: str,
    worker_id: str,
) -> Tuple[int, str, str]:
    cmd = [
        sys.executable,
        "scripts/run_outbound_worker.py",
        "--worker-id",
        worker_id,
        "--once",
        "--jobs-dir",
        jobs_dir,
        "--calls-dir",
        calls_dir,
        "--attempts-dir",
        attempts_dir,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _serialize_jobs_for_metrics(store: JsonJobStore) -> List[dict]:
    rows = []
    for job in store.list_jobs():
        row = asdict(job)
        row["trigger_source"] = job.trigger_source.value
        row["state"] = job.state.value
        rows.append(row)
    return rows


def run_smoke(
    *,
    jobs_dir: str,
    calls_dir: str,
    attempts_dir: str,
    worker_id: str,
) -> int:
    job_store = JsonJobStore(root_dir=jobs_dir)
    attempts_store = JsonContactAttemptStore(root_dir=attempts_dir)

    policy = CallPolicySnapshot(
        timezone="America/Chicago",
        allowed_local_time_ranges=["00:00-23:59"],
        daily_attempt_cap=5,
        min_gap_minutes=1,
    )

    allowed_payload = OutboundCallPayload(
        account_ref="acct_smoke_allow_001",
        party_profile={"target_name": "Alex Morgan", "callback_number": "+1 (555) 010-2000"},
        account_context_ref="ctx_smoke_allow_001",
        suppression_flags={"dnc": False, "cease_contact": False, "legal_hold": False},
    )
    blocked_payload = OutboundCallPayload(
        account_ref="acct_smoke_dnc_001",
        party_profile={"target_name": "Alex Morgan", "callback_number": "+1 (555) 010-2000"},
        account_context_ref="ctx_smoke_dnc_001",
        suppression_flags={"dnc": True, "cease_contact": False, "legal_hold": False},
    )

    allow_job, _ = job_store.enqueue_job(
        trigger_source=TriggerSource.MANUAL,
        campaign_id="cmp_smoke_worker",
        payload=allowed_payload,
        policy=policy,
        priority=-1000,
    )
    dnc_job, _ = job_store.enqueue_job(
        trigger_source=TriggerSource.MANUAL,
        campaign_id="cmp_smoke_worker",
        payload=blocked_payload,
        policy=policy,
        priority=-999,
    )

    rc1, out1, err1 = _run_worker_once(
        jobs_dir=jobs_dir,
        calls_dir=calls_dir,
        attempts_dir=attempts_dir,
        worker_id=worker_id,
    )
    if rc1 != 0:
        print(f"FAIL worker run 1 rc={rc1}")
        if out1:
            print(out1)
        if err1:
            print(err1)
        return 1

    rc2, out2, err2 = _run_worker_once(
        jobs_dir=jobs_dir,
        calls_dir=calls_dir,
        attempts_dir=attempts_dir,
        worker_id=worker_id,
    )
    if rc2 != 0:
        print(f"FAIL worker run 2 rc={rc2}")
        if out2:
            print(out2)
        if err2:
            print(err2)
        return 1

    allow = job_store.get_job(allow_job.job_id)
    dnc = job_store.get_job(dnc_job.job_id)
    allow_events = attempts_store.list_events("acct_smoke_allow_001")
    dnc_events = attempts_store.list_events("acct_smoke_dnc_001")

    errors: List[str] = []
    if allow.state.value != "succeeded":
        errors.append(f"allowed job state expected=succeeded actual={allow.state.value}")
    if len(allow.attempts) != 1:
        errors.append(f"allowed job attempts expected=1 actual={len(allow.attempts)}")
    if not allow.attempts or allow.attempts[-1].outcome_code != "call_initialized":
        errors.append("allowed job last outcome expected=call_initialized")

    if dnc.state.value != "canceled":
        errors.append(f"dnc job state expected=canceled actual={dnc.state.value}")
    if dnc.failure_reason != "blocked_suppression_dnc":
        errors.append(
            f"dnc job failure_reason expected=blocked_suppression_dnc actual={dnc.failure_reason}"
        )
    if len(dnc.attempts) != 0:
        errors.append(f"dnc job attempts expected=0 actual={len(dnc.attempts)}")

    if not allow_events:
        errors.append("missing attempt ledger event for allowed account")
    elif not bool(allow_events[-1].get("counts_toward_attempt", False)):
        errors.append("allowed event should count toward attempts")

    if not dnc_events:
        errors.append("missing attempt ledger event for dnc account")
    else:
        if dnc_events[-1].get("decision_code") != "blocked_suppression_dnc":
            errors.append("dnc event decision_code expected=blocked_suppression_dnc")
        if bool(dnc_events[-1].get("counts_toward_attempt", False)):
            errors.append("dnc event must not count toward attempts")

    job_metrics = build_job_metrics_summary(
        _serialize_jobs_for_metrics(job_store),
        attempt_events=attempts_store.list_recent_events(limit=200),
    )
    if int(job_metrics.get("blocked_suppression_total", 0)) < 1:
        errors.append("job metrics blocked_suppression_total should be >= 1")
    if int(job_metrics.get("contact_attempts_total", 0)) < 1:
        errors.append("job metrics contact_attempts_total should be >= 1")

    if errors:
        print("FAIL worker compliance smoke")
        for e in errors:
            print(f"- {e}")
        print("worker_output_1:", out1 or "(empty)")
        print("worker_output_2:", out2 or "(empty)")
        return 1

    print("PASS worker compliance smoke")
    print("worker_output_1:", out1 or "(empty)")
    print("worker_output_2:", out2 or "(empty)")
    print(
        "summary:",
        f"allow_state={allow.state.value},",
        f"dnc_state={dnc.state.value},",
        f"blocked_suppression_total={job_metrics.get('blocked_suppression_total')},",
        f"contact_attempts_total={job_metrics.get('contact_attempts_total')}",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="One-command smoke test for worker compliance gating.")
    parser.add_argument("--worker-id", default="worker_smoke")
    parser.add_argument("--jobs-dir", default=None, help="Optional override for job store directory.")
    parser.add_argument("--calls-dir", default=None, help="Optional override for call store directory.")
    parser.add_argument("--attempts-dir", default=None, help="Optional override for attempt store directory.")
    args = parser.parse_args()

    if args.jobs_dir or args.calls_dir or args.attempts_dir:
        jobs_dir = args.jobs_dir or "runtime/jobs"
        calls_dir = args.calls_dir or "runtime/calls"
        attempts_dir = args.attempts_dir or "runtime/attempts"
        return run_smoke(
            jobs_dir=jobs_dir,
            calls_dir=calls_dir,
            attempts_dir=attempts_dir,
            worker_id=args.worker_id,
        )

    with tempfile.TemporaryDirectory() as jobs_dir, tempfile.TemporaryDirectory() as calls_dir, tempfile.TemporaryDirectory() as attempts_dir:
        return run_smoke(
            jobs_dir=jobs_dir,
            calls_dir=calls_dir,
            attempts_dir=attempts_dir,
            worker_id=args.worker_id,
        )


if __name__ == "__main__":
    raise SystemExit(main())

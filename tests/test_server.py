from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from src.api import server
from src.api.call_store import JsonCallStore
from src.api.compliance import evaluate_pre_dial_gate
from src.api.contact_attempt_store import JsonContactAttemptStore
from src.api.job_store import JsonJobStore
from src.api.outbound_orchestration import CallPolicySnapshot


def _default_context():
    party_profile = {
        "target_name": "Alex Morgan",
        "target_preferred_name": "Alex",
        "callback_number": "+1 (555) 010-2000",
    }
    account_context = {
        "case_id": "CASE_TEST_001",
        "amount_due": "240.00",
        "currency": "USD",
        "expected_zip": "78701",
        "expected_full_name": "Alex Morgan",
    }
    policy_config = {
        "brand_name": "Northstar Recovery",
        "agent_identity": "debt_collector",
        "disclosures": {
            "post_verification_disclosure_text": (
                "This is Northstar Recovery. This is an attempt to collect a debt, "
                "and any information obtained will be used for that purpose."
            ),
        },
        "verification": {
            "allowed_verification_methods": ["confirm_zip"],
            "max_verification_attempts": 3,
        },
        "limits": {
            "max_total_turns": 25,
            "max_negotiation_proposals": 3,
            "max_reconduction_attempts": 2,
            "max_silence_prompts": 2,
        },
    }
    return party_profile, account_context, policy_config


def _turn_event(transcript: str) -> dict:
    return {
        "event_type": "user_utterance",
        "transcript": transcript,
        "timestamp_utc": "2026-02-08T16:00:00+00:00",
        "current_local_date": "2026-02-08",
        "current_local_time": "10:00",
        "timezone": "America/Chicago",
        "language": "en-US",
    }


class ServerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._orig_call_store = server.call_store
        self._orig_job_store = server.job_store
        self._orig_attempt_store = server.attempt_store

        server.call_store = JsonCallStore(root_dir=root / "calls")
        server.job_store = JsonJobStore(root_dir=root / "jobs")
        server.attempt_store = JsonContactAttemptStore(root_dir=root / "attempts")
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        server.call_store = self._orig_call_store
        server.job_store = self._orig_job_store
        server.attempt_store = self._orig_attempt_store
        self._tmpdir.cleanup()

    def test_health_endpoint(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "collections-voice-agent")
        self.assertFalse(body["llm_in_core_path"])

    def test_call_lifecycle_and_error_codes(self):
        party_profile, account_context, policy_config = _default_context()

        start_resp = self.client.post(
            "/call/start",
            json={"party_profile": party_profile, "account_ref": "acct_life_1"},
        )
        self.assertEqual(start_resp.status_code, 200)
        call_id = start_resp.json()["call_id"]

        for utterance in ("This is Alex Morgan.", "78701", "End of month."):
            turn_resp = self.client.post(
                "/call/turn",
                json={
                    "call_id": call_id,
                    "turn_event": _turn_event(utterance),
                    "party_profile": party_profile,
                    "account_context": account_context,
                    "policy_config": policy_config,
                },
            )
            self.assertEqual(turn_resp.status_code, 200)

        summary_resp = self.client.get(f"/call/{call_id}")
        self.assertEqual(summary_resp.status_code, 200)
        summary = summary_resp.json()
        self.assertEqual(summary["status"], "ended")
        self.assertEqual(summary["account_ref"], "acct_life_1")
        self.assertEqual(summary["final_outcome_code"], "ptp_set")
        self.assertEqual(summary["last_call_state"]["last_user_utterance"], "")

        # Unknown call -> 404
        missing_resp = self.client.get("/call/nonexistent_call_id")
        self.assertEqual(missing_resp.status_code, 404)

        # Invalid / path-traversal call_id -> 400
        bad_get = self.client.get("/call/bad.id")
        self.assertEqual(bad_get.status_code, 400)

        bad_turn = self.client.post(
            "/call/turn",
            json={
                "call_id": "../evil",
                "turn_event": _turn_event("hello"),
                "party_profile": party_profile,
                "account_context": account_context,
                "policy_config": policy_config,
            },
        )
        self.assertEqual(bad_turn.status_code, 400)

    def test_jobs_attempts_and_metrics_endpoints(self):
        job_payload = {
            "trigger_source": "manual",
            "campaign_id": "cmp_api_1",
            "account_ref": "acct_job_1",
            "party_profile": {"target_name": "Alex Morgan", "callback_number": "+1 (555) 010-2000"},
            "account_context_ref": "ctx_1",
            "scheduled_for_utc": "2020-01-01T00:00:00+00:00",
        }
        enq1 = self.client.post("/jobs/enqueue", json=job_payload)
        self.assertEqual(enq1.status_code, 200)
        self.assertTrue(enq1.json()["created"])
        job_id = enq1.json()["job"]["job_id"]

        # Deduplication
        enq2 = self.client.post("/jobs/enqueue", json=job_payload)
        self.assertEqual(enq2.status_code, 200)
        self.assertFalse(enq2.json()["created"])
        self.assertEqual(enq2.json()["job"]["job_id"], job_id)

        listed = self.client.get("/jobs")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)

        leased = self.client.post("/jobs/lease", json={"worker_id": "worker_api", "lease_seconds": 60})
        self.assertEqual(leased.status_code, 200)
        self.assertEqual(leased.json()["job"]["job_id"], job_id)

        started = self.client.post(f"/jobs/{job_id}/start")
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["state"], "running")

        succeeded = self.client.post(f"/jobs/{job_id}/success", json={"outcome_code": "ptp_set", "call_id": "call_1"})
        self.assertEqual(succeeded.status_code, 200)
        self.assertEqual(succeeded.json()["state"], "succeeded")

        # Enqueue a second job to exercise /jobs/{id}/failure
        fail_payload = dict(job_payload, account_ref="acct_job_2", idempotency_key="fail_job_key")
        fail_enq = self.client.post("/jobs/enqueue", json=fail_payload)
        fail_id = fail_enq.json()["job"]["job_id"]
        self.client.post("/jobs/lease", json={"worker_id": "worker_api", "lease_seconds": 60})
        self.client.post(f"/jobs/{fail_id}/start")
        failed = self.client.post(f"/jobs/{fail_id}/failure", json={"error_code": "dial_timeout"})
        self.assertEqual(failed.status_code, 200)
        self.assertEqual(failed.json()["state"], "waiting_retry")

        # Invalid job_id -> 400
        self.assertEqual(self.client.get("/jobs/bad.job").status_code, 400)
        self.assertEqual(self.client.post("/jobs/bad.job/start").status_code, 400)

        attempts = self.client.get("/attempts/acct_job_1")
        self.assertEqual(attempts.status_code, 200)
        self.assertEqual(attempts.json()["account_ref"], "acct_job_1")

        metrics = self.client.get("/metrics/summary")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("jobs", metrics.json())

    def test_in_call_cease_contact_persists_suppression_and_blocks_future_dials(self):
        party_profile, account_context, policy_config = _default_context()
        account_ref = "acct_cease_1"

        start_resp = self.client.post(
            "/call/start",
            json={"party_profile": party_profile, "account_ref": account_ref},
        )
        self.assertEqual(start_resp.status_code, 200)
        call_id = start_resp.json()["call_id"]

        stop_resp = self.client.post(
            "/call/turn",
            json={
                "call_id": call_id,
                "turn_event": _turn_event("stop calling me"),
                "party_profile": party_profile,
                "account_context": account_context,
                "policy_config": policy_config,
            },
        )
        self.assertEqual(stop_resp.status_code, 200)
        self.assertTrue(stop_resp.json()["call_state"]["cease_contact_requested"])

        suppressions = server.attempt_store.get_suppressions(account_ref)
        self.assertTrue(suppressions["cease_contact"])

        policy = CallPolicySnapshot(
            timezone="America/Chicago",
            allowed_local_time_ranges=["08:00-20:00"],
            daily_attempt_cap=2,
            min_gap_minutes=60,
        )
        decision = evaluate_pre_dial_gate(
            account_ref=account_ref,
            policy=policy,
            suppression_flags={"dnc": False, "cease_contact": False, "legal_hold": False},
            attempt_store=server.attempt_store,
            now_utc=datetime(2026, 2, 8, 16, 0, tzinfo=UTC),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "blocked_suppression_cease_contact")
        self.assertFalse(decision.retryable)


if __name__ == "__main__":
    unittest.main()

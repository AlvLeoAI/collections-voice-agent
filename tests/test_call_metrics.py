from __future__ import annotations

import unittest

from src.api.metrics import build_job_metrics_summary, build_metrics_summary


class CallMetricsTests(unittest.TestCase):
    def test_build_metrics_summary_counts_rates_and_timing(self):
        records = [
            {
                "status": "ended",
                "created_at_utc": "2026-02-06T21:00:00+00:00",
                "updated_at_utc": "2026-02-06T21:05:00+00:00",
                "final_outcome_code": "ptp_set",
                "turns": [
                    {
                        "actions": [{"action": "set_outcome", "outcome_code": "ptp_set"}],
                        "recorded_at_utc": "2026-02-06T21:02:00+00:00",
                    }
                ],
            },
            {
                "status": "ended",
                "created_at_utc": "2026-02-06T22:00:00+00:00",
                "updated_at_utc": "2026-02-06T22:03:00+00:00",
                "final_outcome_code": "wrong_party",
                "turns": [],
            },
            {
                "status": "active",
                "created_at_utc": "2026-02-06T23:00:00+00:00",
                "updated_at_utc": "2026-02-06T23:01:00+00:00",
                "final_outcome_code": None,
                "last_call_state": {"promise_to_pay": {"confirmed": True}},
                "turns": [],
            },
        ]

        summary = build_metrics_summary(records, trend_days=14)

        self.assertEqual(summary["calls_total"], 3)
        self.assertEqual(summary["active_calls"], 1)
        self.assertEqual(summary["ended_calls"], 2)
        self.assertEqual(summary["ptp_calls_total"], 2)
        self.assertEqual(summary["ptp_calls_ended"], 1)
        self.assertEqual(summary["ptp_success_rate_ended"], 0.5)
        self.assertEqual(summary["time_to_ptp_samples"], 1)
        self.assertEqual(summary["avg_time_to_ptp_seconds"], 120.0)
        self.assertEqual(summary["median_time_to_ptp_seconds"], 120.0)
        self.assertEqual(summary["avg_time_to_ptp_minutes"], 2.0)

        daily = summary["daily"]
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["date"], "2026-02-06")
        self.assertEqual(daily[0]["calls_total"], 3)
        self.assertEqual(daily[0]["ended_calls"], 2)
        self.assertEqual(daily[0]["ptp_calls_ended"], 1)
        self.assertEqual(daily[0]["ptp_success_rate_ended"], 0.5)

    def test_ptp_timing_falls_back_to_updated_at_when_needed(self):
        records = [
            {
                "status": "ended",
                "created_at_utc": "2026-02-06T21:00:00+00:00",
                "updated_at_utc": "2026-02-06T21:03:30+00:00",
                "final_outcome_code": "ptp_set",
                "turns": [{"actions": [{"action": "set_outcome", "outcome_code": "ptp_set"}]}],
            }
        ]

        summary = build_metrics_summary(records)
        self.assertEqual(summary["ptp_calls_ended"], 1)
        self.assertEqual(summary["time_to_ptp_samples"], 1)
        self.assertEqual(summary["avg_time_to_ptp_seconds"], 210.0)

    def test_build_job_metrics_summary_includes_blocked_counts(self):
        jobs = [
            {
                "state": "succeeded",
                "attempts": [{"outcome_code": "blocked_suppression_dnc", "error_code": None}],
                "failure_reason": None,
            },
            {
                "state": "waiting_retry",
                "attempts": [{"outcome_code": None, "error_code": "blocked_policy_min_gap"}],
                "failure_reason": None,
            },
            {
                "state": "canceled",
                "attempts": [],
                "failure_reason": "blocked_suppression_legal_hold",
            },
        ]
        attempts = [
            {"decision_code": "blocked_suppression_dnc", "counts_toward_attempt": False},
            {"decision_code": "call_initialized", "counts_toward_attempt": True},
        ]

        report = build_job_metrics_summary(jobs, attempt_events=attempts)
        self.assertEqual(report["jobs_total"], 3)
        self.assertEqual(report["blocked_policy_total"], 1)
        self.assertEqual(report["blocked_suppression_total"], 2)
        self.assertEqual(report["attempt_events_total"], 2)
        self.assertEqual(report["contact_attempts_total"], 1)


if __name__ == "__main__":
    unittest.main()

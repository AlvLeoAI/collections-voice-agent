from __future__ import annotations

import unittest

from scripts.analyze_nlu_report import build_report


class NluReportTests(unittest.TestCase):
    def test_build_report_basic_aggregation(self):
        records = [
            {
                "status": "ended",
                "final_outcome_code": "ptp_set",
                "turns": [
                    {"assistant_intent": "request_target", "nlu_intent": "affirmation", "nlu_confidence": 0.72},
                    {"assistant_intent": "verify_identity", "nlu_intent": "unknown", "nlu_confidence": 0.30},
                    {"assistant_intent": "close", "nlu_intent": "affirmation", "nlu_confidence": 0.70},
                ],
            }
        ]
        report = build_report(records, low_confidence_threshold=0.45)
        self.assertEqual(report["calls_total"], 1)
        self.assertEqual(report["status_counts"].get("ended"), 1)
        self.assertEqual(report["outcome_counts"].get("ptp_set"), 1)
        self.assertEqual(report["total_turns"], 3)
        self.assertEqual(report["turns_with_nlu"], 3)
        self.assertEqual(report["nlu_intent_counts"].get("affirmation"), 2)
        self.assertEqual(report["nlu_intent_counts"].get("unknown"), 1)
        self.assertEqual(report["low_confidence_counts"].get("unknown"), 1)
        self.assertEqual(report["intent_to_assistant"]["affirmation"]["request_target"], 1)
        self.assertEqual(report["intent_to_assistant"]["affirmation"]["close"], 1)


if __name__ == "__main__":
    unittest.main()


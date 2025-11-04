from __future__ import annotations

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.outbound_voice_agent import CallState, handle_turn, start_call
from src.outbound_voice_agent.agent import TurnEvent, _enforce_voice_first


def default_context():
    party_profile = {
        "target_name": "Alex Morgan",
        "target_preferred_name": "Alex",
        "callback_number": "+1 (555) 010-2000",
    }

    account_context = {
        "case_id": "CASE_DEMO_001",
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
            "allowed_verification_methods": ["confirm_zip", "confirm_full_name"],
            "max_verification_attempts": 3,
        },
        "limits": {
            "max_total_turns": 25,
            "max_negotiation_proposals": 3,
            "max_reconduction_attempts": 2,
            "max_silence_prompts": 2,
        },
        "call_windows": {"allowed_local_time_ranges": []},
        "handoff": {
            "human_escalation_available": True,
            "escalation_reason_codes": ["dispute", "verification_failed", "user_requested_human"],
        },
    }
    return party_profile, account_context, policy_config


def mk_event(text: str | None, *, event_type: str = "user_utterance") -> TurnEvent:
    tz = ZoneInfo("America/Chicago")
    now_local = datetime(2026, 2, 6, 10, 0, tzinfo=tz)
    now_utc = now_local.astimezone(timezone.utc)
    return TurnEvent(
        event_type=event_type,  # type: ignore[arg-type]
        transcript=text,
        timestamp_utc=now_utc.isoformat(),
        current_local_date=now_local.date().isoformat(),
        current_local_time=now_local.strftime("%H:%M"),
        timezone="America/Chicago",
        language="en-US",
    )


class OutboundAgentRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.party_profile, self.account_context, self.policy_config = default_context()
        self.call_state = CallState()
        initial = start_call(call_state=self.call_state, party_profile=self.party_profile)
        self.call_state = initial["call_state"]

    def step(self, text: str | None, *, event_type: str = "user_utterance"):
        result = handle_turn(
            turn_event=mk_event(text, event_type=event_type),
            call_state=self.call_state,
            party_profile=self.party_profile,
            account_context=self.account_context,
            policy_config=self.policy_config,
        )
        self.call_state = result["call_state"]
        return result

    def test_weekday_date_requires_confirmation_before_ptp(self):
        self.step("This is Alex Morgan.")
        self.step("78701.")

        confirm_turn = self.step("Friday.")
        self.assertEqual(confirm_turn["assistant_intent"], "confirm_payment_date")
        self.assertEqual(self.call_state.phase, "post_verification")
        self.assertFalse(self.call_state.promise_to_pay.confirmed)
        self.assertEqual(confirm_turn["actions"], [])

        close_turn = self.step("Yes.")
        self.assertEqual(close_turn["assistant_intent"], "close")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertTrue(self.call_state.promise_to_pay.confirmed)
        self.assertEqual(self.call_state.end_reason, "ptp_set")

        actions = close_turn["actions"]
        self.assertTrue(any(a.get("action") == "set_outcome" and a.get("outcome_code") == "ptp_set" for a in actions))
        self.assertTrue(any(a.get("action") == "end_call" and a.get("reason") == "ptp_set" for a in actions))

    def test_wrong_party_sets_explicit_end_reason_and_outcome(self):
        result = self.step("Wrong number. Alex does not live here.")
        self.assertEqual(result["assistant_intent"], "close")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "wrong_party")

        actions = result["actions"]
        self.assertTrue(any(a.get("action") == "set_outcome" and a.get("outcome_code") == "wrong_party" for a in actions))
        self.assertTrue(any(a.get("action") == "end_call" and a.get("reason") == "wrong_party" for a in actions))

    def test_dispute_escalation_sets_explicit_outcome(self):
        self.step("This is Alex Morgan.")
        self.step("78701.")
        result = self.step("I don't owe this debt.")

        self.assertEqual(result["assistant_intent"], "escalate")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "escalated_dispute")

        actions = result["actions"]
        self.assertTrue(any(a.get("action") == "set_outcome" and a.get("outcome_code") == "escalated_dispute" for a in actions))
        self.assertTrue(any(a.get("action") == "escalate_to_human" and a.get("reason") == "dispute" for a in actions))
        self.assertTrue(any(a.get("action") == "end_call" and a.get("reason") == "escalated_dispute" for a in actions))

    def test_silence_timeout_sets_explicit_outcome(self):
        self.step(None, event_type="silence")
        self.step(None, event_type="silence")
        result = self.step(None, event_type="silence")

        self.assertEqual(result["assistant_intent"], "close")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "silence_timeout")

        actions = result["actions"]
        self.assertTrue(any(a.get("action") == "set_outcome" and a.get("outcome_code") == "silence_timeout" for a in actions))
        self.assertTrue(any(a.get("action") == "end_call" and a.get("reason") == "silence_timeout" for a in actions))

    def test_voice_first_guard_limits_questions_and_sentences(self):
        text = "First sentence? Second sentence? Third sentence."
        constrained = _enforce_voice_first(text)
        self.assertLessEqual(constrained.count("?"), 1)

        sentence_chunks = [s.strip() for s in constrained.replace("?", ".").replace("!", ".").split(".") if s.strip()]
        self.assertLessEqual(len(sentence_chunks), 2)

    def test_low_confidence_clarify_then_escalate(self):
        first = self.step("uhh... static...")
        self.assertEqual(first["assistant_intent"], "request_target")
        self.assertEqual(self.call_state.phase, "pre_verification")

        second = self.step("... ??? ...")
        self.assertEqual(second["assistant_intent"], "escalate")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "escalated_low_confidence")
        self.assertTrue(any(a.get("action") == "set_outcome" and a.get("outcome_code") == "escalated_low_confidence" for a in second["actions"]))

    def test_goodbye_ends_call(self):
        result = self.step("ok bye bye")
        self.assertEqual(result["assistant_intent"], "close")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "user_ended")
        self.assertTrue(any(a.get("action") == "set_outcome" and a.get("outcome_code") == "user_ended" for a in result["actions"]))

    def test_already_closed_is_idempotent(self):
        self.step("ok bye bye")
        follow_up = self.step("still here?")
        self.assertEqual(follow_up["assistant_intent"], "already_closed")
        self.assertEqual(follow_up["actions"], [])

    def test_verification_why_gets_privacy_explanation(self):
        self.step("This is Alex Morgan.")
        result = self.step("why?")
        self.assertEqual(result["assistant_intent"], "verify_identity")
        self.assertIn("protect your privacy", result["assistant_text"].lower())

    def test_verification_accepts_split_numeric_zip(self):
        self.step("Yeah, I'm Alex Morgan.")
        result = self.step("it's 78 and 701")
        self.assertEqual(result["assistant_intent"], "deliver_disclosure")
        self.assertTrue(self.call_state.right_party_verified)
        self.assertEqual(self.call_state.phase, "post_verification")

    def test_verification_accepts_spoken_digit_zip(self):
        self.step("Yeah, I'm Alex Morgan.")
        result = self.step("It's seven eight seven zero one")
        self.assertEqual(result["assistant_intent"], "deliver_disclosure")
        self.assertTrue(self.call_state.right_party_verified)
        self.assertEqual(self.call_state.phase, "post_verification")

    def test_verification_accepts_full_number_word_zip(self):
        self.step("Yeah, I'm Alex Morgan.")
        result = self.step("sure it's seventy eight thousand and seven hundred and one")
        self.assertEqual(result["assistant_intent"], "deliver_disclosure")
        self.assertTrue(self.call_state.right_party_verified)
        self.assertEqual(self.call_state.phase, "post_verification")

    def test_affirmation_after_disclosure_sets_today_ptp(self):
        self.step("Yeah, I'm Alex Morgan.")
        self.step("78701")
        result = self.step("Yes, sure.")

        self.assertEqual(result["assistant_intent"], "close")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "ptp_set")
        self.assertEqual(self.call_state.promise_to_pay.date, "2026-02-06")
        self.assertTrue(self.call_state.promise_to_pay.confirmed)

    def test_affirmative_paraphrase_after_disclosure_sets_today_ptp(self):
        self.step("Yeah, I'm Alex Morgan.")
        self.step("78701")
        result = self.step("Sure, I can pay the balance today.")

        self.assertEqual(result["assistant_intent"], "close")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "ptp_set")
        self.assertEqual(self.call_state.promise_to_pay.date, "2026-02-06")
        self.assertTrue(self.call_state.promise_to_pay.confirmed)

    def test_generic_yes_after_exact_date_prompt_requests_concrete_date(self):
        self.step("Yeah, I'm Alex Morgan.")
        self.step("78701")
        self.step("No, not today.")
        result = self.step("Yes, I can.")

        self.assertEqual(result["assistant_intent"], "negotiate")
        self.assertIn("exact payment date", result["assistant_text"].lower())
        self.assertEqual(self.call_state.phase, "post_verification")


if __name__ == "__main__":
    unittest.main()

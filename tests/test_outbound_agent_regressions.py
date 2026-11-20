from __future__ import annotations

import unittest
from datetime import UTC, datetime
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


def mk_event(
    text: str | None,
    *,
    event_type: str = "user_utterance",
    local_date: tuple[int, int, int] = (2026, 2, 6),
) -> TurnEvent:
    tz = ZoneInfo("America/Chicago")
    year, month, day = local_date
    now_local = datetime(year, month, day, 10, 0, tzinfo=tz)
    now_utc = now_local.astimezone(UTC)
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

    def step(
        self,
        text: str | None,
        *,
        event_type: str = "user_utterance",
        local_date: tuple[int, int, int] = (2026, 2, 6),
    ):
        result = handle_turn(
            turn_event=mk_event(text, event_type=event_type, local_date=local_date),
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

    def test_cant_afford_after_disclosure_does_not_create_ptp(self):
        self.step("This is Alex Morgan.")
        self.step("78701")
        result = self.step("I can't afford it")

        self.assertEqual(result["assistant_intent"], "negotiate")
        self.assertEqual(self.call_state.phase, "post_verification")
        self.assertFalse(self.call_state.promise_to_pay.confirmed)
        self.assertIsNone(self.call_state.promise_to_pay.date)
        self.assertEqual(result["actions"], [])

    def test_wrong_person_in_pre_verification_and_verification_closes_as_wrong_party(self):
        pre_result = self.step("You have the wrong person")
        self.assertEqual(pre_result["assistant_intent"], "close")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "wrong_party")
        self.assertFalse(self.call_state.escalation_flag)
        self.assertNotIn("debt", pre_result["assistant_text"].lower())
        self.assertNotIn("240", pre_result["assistant_text"].lower())
        self.assertNotIn("northstar", pre_result["assistant_text"].lower())

        # Reset and test wrong_party during verification phase
        self.setUp()
        self.step("Yes, speaking.")
        self.assertEqual(self.call_state.phase, "verification")
        ver_result = self.step("You have the wrong person")
        self.assertEqual(ver_result["assistant_intent"], "close")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "wrong_party")
        self.assertFalse(self.call_state.escalation_flag)
        self.assertNotIn("debt", ver_result["assistant_text"].lower())
        self.assertNotIn("240", ver_result["assistant_text"].lower())
        self.assertNotIn("northstar", ver_result["assistant_text"].lower())

    def test_past_dates_rejected_on_reference_date(self):
        ref_date = (2026, 2, 20)
        for utterance in ("February 3", "2026-02-03"):
            with self.subTest(utterance=utterance):
                self.setUp()
                self.step("This is Alex Morgan.", local_date=ref_date)
                self.step("78701", local_date=ref_date)
                result = self.step(utterance, local_date=ref_date)

                self.assertEqual(result["assistant_intent"], "negotiate")
                self.assertEqual(self.call_state.phase, "post_verification")
                self.assertFalse(self.call_state.promise_to_pay.confirmed)
                self.assertIsNone(self.call_state.promise_to_pay.date)
                self.assertIn("already passed", result["assistant_text"].lower())

    def test_deferred_date_confirms_ptp_both_immediately_and_after_no(self):
        ref_date = (2026, 2, 20)

        # Immediately after disclosure
        self.step("This is Alex Morgan.", local_date=ref_date)
        self.step("78701", local_date=ref_date)
        immediate = self.step("I can pay later, on February 25", local_date=ref_date)
        self.assertEqual(immediate["assistant_intent"], "close")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "ptp_set")
        self.assertTrue(self.call_state.promise_to_pay.confirmed)
        self.assertEqual(self.call_state.promise_to_pay.date, "2026-02-25")

        # After initial "No, not today."
        self.setUp()
        self.step("This is Alex Morgan.", local_date=ref_date)
        self.step("78701", local_date=ref_date)
        self.step("No, not today.", local_date=ref_date)
        after_no = self.step("I can pay later, on February 25", local_date=ref_date)
        self.assertEqual(after_no["assistant_intent"], "close")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "ptp_set")
        self.assertTrue(self.call_state.promise_to_pay.confirmed)
        self.assertEqual(self.call_state.promise_to_pay.date, "2026-02-25")

    def test_privacy_pushback_preserves_question_and_fits_voice_first(self):
        self.step("This is Alex Morgan.")
        uncomfortable = self.step("why should I give you my zip code?")
        text_u = uncomfortable["assistant_text"]
        self.assertIn("?", text_u)
        self.assertLessEqual(text_u.count("?"), 1)
        sentences_u = [s.strip() for s in text_u.replace("?", ".").replace("!", ".").split(".") if s.strip()]
        self.assertLessEqual(len(sentences_u), 2)
        self.assertEqual(self.call_state.last_assistant_question, text_u)

        self.setUp()
        self.step("This is Alex Morgan.")
        why_result = self.step("why?")
        text_w = why_result["assistant_text"]
        self.assertIn("zip code", text_w.lower())
        self.assertIn("protect your privacy", text_w.lower())
        self.assertLessEqual(text_w.count("?"), 1)
        sentences_w = [s.strip() for s in text_w.replace("?", ".").replace("!", ".").split(".") if s.strip()]
        self.assertLessEqual(len(sentences_w), 2)
        self.assertEqual(self.call_state.last_assistant_question, text_w if "?" in text_w else "")

    def test_dynamic_partial_offer_scales_balance_and_clamps_month_end(self):
        self.account_context["amount_due"] = "80.00"
        ref_date = (2026, 2, 27)
        self.step("This is Alex Morgan.", local_date=ref_date)
        self.step("78701", local_date=ref_date)
        self.step("No, not today.", local_date=ref_date)
        offer = self.step("No.", local_date=ref_date)

        self.assertEqual(offer["assistant_intent"], "confirm_payment_date")
        self.assertIn("$40.00", offer["assistant_text"])
        self.assertIn("February 28", offer["assistant_text"])
        self.assertNotIn("120.00", offer["assistant_text"])
        self.assertNotIn("25th", offer["assistant_text"])
        self.assertEqual(self.call_state.last_proposed_payment_amount, "40.00")
        self.assertEqual(self.call_state.last_proposed_payment_date, "2026-02-28")

        confirm = self.step("Yes", local_date=ref_date)
        self.assertEqual(confirm["assistant_intent"], "close")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "ptp_set")
        self.assertTrue(self.call_state.promise_to_pay.confirmed)
        self.assertEqual(self.call_state.promise_to_pay.amount, "40.00")
        self.assertEqual(self.call_state.promise_to_pay.date, "2026-02-28")

    def test_non_english_si_clarifies_then_escalates(self):
        first = self.step("sí")
        self.assertEqual(first["assistant_intent"], "request_target")
        self.assertEqual(self.call_state.phase, "pre_verification")

        second = self.step("sí")
        self.assertEqual(second["assistant_intent"], "escalate")
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "escalated_low_confidence")

    def test_stop_calling_me_sets_cease_contact_and_emits_dnc_action(self):
        result = self.step("stop calling me")
        self.assertTrue(self.call_state.cease_contact_requested)
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "cease_contact")
        self.assertEqual(self.call_state.last_user_utterance, "")
        self.assertTrue(
            any(
                a.get("action") == "mark_do_not_contact"
                and a.get("scope") == "account"
                and a.get("reason") == "user_requested_cease_contact"
                for a in result["actions"]
            )
        )

    def test_last_user_utterance_remains_empty_throughout_call(self):
        self.assertEqual(self.call_state.last_user_utterance, "")
        self.step("This is Alex Morgan.")
        self.assertEqual(self.call_state.last_user_utterance, "")
        self.step("78701")
        self.assertEqual(self.call_state.last_user_utterance, "")
        self.step("No, not today.")
        self.assertEqual(self.call_state.last_user_utterance, "")
        self.step("February 20")
        self.assertEqual(self.call_state.last_user_utterance, "")

    def test_next_month_out_of_month_date_reconducts_before_busy(self):
        ref_date = (2026, 2, 4)
        self.step("This is Alex Morgan.", local_date=ref_date)
        self.step("78701", local_date=ref_date)
        result = self.step("Not a good time, and I can pay next month.", local_date=ref_date)
        self.assertEqual(result["assistant_intent"], "negotiate")
        self.assertEqual(self.call_state.phase, "post_verification")
        self.assertIn("end of this month", result["assistant_text"].lower())

    def test_refusal_after_partial_offer_escalates_with_flag(self):
        self.account_context["amount_due"] = "80.00"
        ref_date = (2026, 2, 27)
        self.step("This is Alex Morgan.", local_date=ref_date)
        self.step("78701", local_date=ref_date)
        self.step("No, not today.", local_date=ref_date)
        offer = self.step("No.", local_date=ref_date)
        self.assertEqual(offer["assistant_intent"], "confirm_payment_date")

        refusal = self.step("I can't afford that either", local_date=ref_date)
        self.assertEqual(refusal["assistant_intent"], "escalate")
        self.assertTrue(self.call_state.escalation_flag)
        self.assertEqual(self.call_state.phase, "ended")
        self.assertEqual(self.call_state.end_reason, "escalated_hard_refusal")


if __name__ == "__main__":
    unittest.main()

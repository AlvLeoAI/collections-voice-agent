from __future__ import annotations

import unittest

from src.outbound_voice_agent.intent_classifier import classify_utterance, is_low_confidence_unknown


class IntentClassifierTests(unittest.TestCase):
    def test_affirmation_paraphrase(self):
        result = classify_utterance("Yup, that's me.")
        self.assertEqual(result.primary_intent, "affirmation")
        self.assertGreaterEqual(result.confidence, 0.5)

    def test_wrong_party_paraphrase(self):
        result = classify_utterance("He moved out, wrong number.")
        self.assertEqual(result.primary_intent, "wrong_party")
        self.assertGreaterEqual(result.confidence, 0.5)

    def test_human_handoff_request(self):
        result = classify_utterance("Can I talk to a real person?")
        self.assertEqual(result.primary_intent, "human_handoff")
        self.assertGreaterEqual(result.confidence, 0.5)

    def test_goodbye_detected(self):
        result = classify_utterance("ok bye bye")
        self.assertEqual(result.primary_intent, "goodbye")
        self.assertGreaterEqual(result.confidence, 0.5)

    def test_noisy_unknown_low_confidence(self):
        result = classify_utterance("uhm... static... what?")
        self.assertEqual(result.primary_intent, "unknown")
        self.assertTrue(is_low_confidence_unknown(result))

    def test_why_zip_maps_to_uncomfortable(self):
        result = classify_utterance("why should I give you my zip code?")
        self.assertEqual(result.primary_intent, "uncomfortable")
        self.assertGreaterEqual(result.confidence, 0.5)

    def test_single_word_why_maps_to_identity_question(self):
        result = classify_utterance("why?")
        self.assertEqual(result.primary_intent, "identity_question")
        self.assertGreaterEqual(result.confidence, 0.5)

    def test_ambiguous_yes_no_is_unknown(self):
        result = classify_utterance("yes... no... maybe")
        self.assertEqual(result.primary_intent, "unknown")
        self.assertTrue(is_low_confidence_unknown(result))

    def test_cant_afford_is_refusal_not_affirmation(self):
        for utterance in ("I can't afford it", "I can\u2019t afford it", "I can not pay"):
            with self.subTest(utterance=utterance):
                result = classify_utterance(utterance)
                self.assertFalse(result.matched("affirmation"))
        for utterance in ("I can't afford it", "I can\u2019t afford it"):
            with self.subTest(refusal=utterance):
                result = classify_utterance(utterance)
                self.assertEqual(result.primary_intent, "refusal")
                self.assertFalse(result.matched("affirmation"))

    def test_wrong_person_is_wrong_party_not_human_handoff(self):
        result = classify_utterance("You have the wrong person")
        self.assertEqual(result.primary_intent, "wrong_party")
        self.assertFalse(result.matched("human_handoff"))

    def test_deferred_payment_date_not_classified_as_busy(self):
        result = classify_utterance("I can pay later, on February 25")
        self.assertFalse(result.matched("busy"))

    def test_explicit_busy_call_me_later_matches_busy(self):
        result = classify_utterance("I'm busy, call me later")
        self.assertEqual(result.primary_intent, "busy")
        self.assertTrue(result.matched("busy"))

    def test_non_english_utterance_is_low_confidence_unknown(self):
        result = classify_utterance("sí")
        self.assertEqual(result.primary_intent, "unknown")
        self.assertTrue(is_low_confidence_unknown(result))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Literal, Pattern


IntentLabel = Literal[
    "stop_request",
    "goodbye",
    "human_handoff",
    "wrong_party",
    "dispute",
    "busy",
    "uncomfortable",
    "refusal",
    "uncertain",
    "identity_question",
    "affirmation",
    "negation",
    "unknown",
]


@dataclass(frozen=True)
class IntentClassification:
    primary_intent: IntentLabel
    confidence: float
    scores: Dict[str, float]
    matched_intents: List[str]

    def matched(self, intent: str, *, threshold: float = 0.5) -> bool:
        return self.scores.get(intent, 0.0) >= threshold

    def to_dict(self) -> Dict[str, object]:
        return {
            "primary_intent": self.primary_intent,
            "confidence": round(self.confidence, 3),
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
            "matched_intents": self.matched_intents,
        }


_PATTERNS: Dict[str, Pattern[str]] = {
    "stop_request": re.compile(r"\b(stop calling|do not call|don't call|cease contact|remove (me|my number)|opt out)\b", re.I),
    "goodbye": re.compile(r"\b(bye|goodbye|bye bye|see you|talk later|gotta go|have to go|end this call|hang up)\b", re.I),
    "human_handoff": re.compile(r"\b(human|representative|agent|person|specialist|operator|talk to (someone|a person|a human|an agent)|real person)\b", re.I),
    "wrong_party": re.compile(
        r"\b(wrong (number|person)|doesn't live|does not live|no longer (at|here)|not (the person|here)|moved out|you reached the wrong)\b",
        re.I,
    ),
    "dispute": re.compile(r"\b(don't owe|do not owe|not my debt|dispute|fraud|mistake|wrong amount)\b", re.I),
    "busy": re.compile(r"\b(not a good time|can't talk|cannot talk|busy|call back|later|in a meeting|driving|call me later)\b", re.I),
    "uncomfortable": re.compile(
        r"\b(not comfortable|why do you need|why should i (give|provide)|won't give|don't want to (provide|give))\b",
        re.I,
    ),
    "refusal": re.compile(r"\b(don't want to pay|not paying|won't pay|refuse|can't pay|not able to pay|never paying|can't afford|no chance)\b", re.I),
    "uncertain": re.compile(r"\b(don't know|not sure|maybe|i'll see|depends|have to check)\b", re.I),
    "identity_question": re.compile(r"\b(who is this|who are you|what is this about|why are you calling|^why\??$)\b", re.I),
    "affirmation": re.compile(r"\b(yes|yeah|yep|yup|correct|that's right|sure|okay|ok|i can|sounds good|absolutely|definitely|go ahead|speaking|this is)\b", re.I),
    "negation": re.compile(r"\b(no|nope|not|cannot|can't|don't|do not|never|incorrect|won't be able to|wont be able to)\b", re.I),
}


_PRIORITY: List[str] = [
    "stop_request",
    "goodbye",
    "human_handoff",
    "wrong_party",
    "dispute",
    "busy",
    "uncomfortable",
    "refusal",
    "uncertain",
    "identity_question",
    "affirmation",
    "negation",
]


_BASE_CONFIDENCE: Dict[str, float] = {
    "stop_request": 0.93,
    "goodbye": 0.9,
    "human_handoff": 0.88,
    "wrong_party": 0.9,
    "dispute": 0.9,
    "busy": 0.82,
    "uncomfortable": 0.75,
    "refusal": 0.86,
    "uncertain": 0.74,
    "identity_question": 0.76,
    "affirmation": 0.72,
    "negation": 0.72,
}


def classify_utterance(text: str) -> IntentClassification:
    normalized = (text or "").strip().lower()
    if not normalized:
        return IntentClassification(primary_intent="unknown", confidence=0.0, scores={}, matched_intents=[])

    normalized_token = normalized.strip(" .!?")

    scores: Dict[str, float] = {}
    matched_intents: List[str] = []
    for label, pattern in _PATTERNS.items():
        if pattern.search(normalized):
            scores[label] = _BASE_CONFIDENCE.get(label, 0.7)
            matched_intents.append(label)
        else:
            scores[label] = 0.0

    if normalized_token == "why":
        scores["identity_question"] = _BASE_CONFIDENCE["identity_question"]
        if "identity_question" not in matched_intents:
            matched_intents.append("identity_question")

    if not matched_intents:
        return IntentClassification(primary_intent="unknown", confidence=0.2, scores=scores, matched_intents=[])

    # Ambiguous yes/no answers are treated as low-confidence unknown unless a
    # stronger business-critical intent is also present.
    strong_intents = {"stop_request", "human_handoff", "wrong_party", "dispute", "busy", "uncomfortable", "refusal"}
    if "affirmation" in matched_intents and "negation" in matched_intents and not (strong_intents & set(matched_intents)):
        return IntentClassification(
            primary_intent="unknown",
            confidence=0.3,
            scores=scores,
            matched_intents=matched_intents,
        )

    primary_intent = "unknown"
    confidence = 0.2
    for label in _PRIORITY:
        if label in matched_intents:
            primary_intent = label
            confidence = scores[label]
            break

    # Reduce confidence when there are competing intents in the same range.
    near_ties = [intent for intent in matched_intents if intent != primary_intent and scores[intent] >= (confidence - 0.08)]
    if near_ties:
        confidence = max(0.35, confidence - 0.15)

    return IntentClassification(
        primary_intent=primary_intent,  # type: ignore[arg-type]
        confidence=confidence,
        scores=scores,
        matched_intents=matched_intents,
    )


def is_low_confidence_unknown(classification: IntentClassification, *, threshold: float = 0.45) -> bool:
    return classification.primary_intent == "unknown" and classification.confidence < threshold

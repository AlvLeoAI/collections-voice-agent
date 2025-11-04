from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


Phase = Literal[
    "pre_verification",
    "verification",
    "post_verification",
    "closing",
    "escalation",
    "ended",
]

YesNoUnknown = Literal["unknown", "yes", "no"]

UserSentiment = Literal["neutral", "upset", "hostile", "confused", "cooperative"]


class PromiseToPay(BaseModel):
    """Promise-to-pay (PTP) state. Never store payment credentials."""

    model_config = ConfigDict(extra="forbid")

    # Promise date in YYYY-MM-DD, if set.
    date: Optional[str] = None
    # Decimal amount as a string (e.g., "50.00"), if set.
    amount: Optional[str] = None
    # True only when the user explicitly confirmed both date and amount.
    confirmed: bool = False


class Callback(BaseModel):
    """Callback scheduling state."""

    model_config = ConfigDict(extra="forbid")

    # True if the user requested or accepted a callback.
    requested: bool = False
    # ISO-8601 local datetime string (confirmed with the user), if set.
    datetime_local: Optional[str] = None


class CallState(BaseModel):
    """
    Authoritative call state for a single outbound collections call.

    Match fields and meanings exactly to the `outbound_voice_agent` SKILL contract.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ----- Required fields -----

    # Current state-machine phase.
    phase: Phase = "pre_verification"

    # Increment on every assistant response.
    turn_count: int = 0

    # True only when right-party verification criteria are met.
    right_party_verified: bool = False

    # Confidence score in [0, 1] supporting right-party verification.
    right_party_confidence: float = 0.0

    # Count of verification attempts (verification questions asked).
    verification_attempts: int = 0

    # Number of consecutive silence events since the last meaningful user utterance.
    silence_count: int = 0

    # Most recent payment date proposed by the assistant (YYYY-MM-DD), if any.
    last_proposed_payment_date: Optional[str] = None

    # True when the call should be handed off or otherwise escalated ASAP.
    escalation_flag: bool = False

    # ----- Recommended fields -----

    # Whether the intended target was reached.
    target_reached: YesNoUnknown = "unknown"

    # Whether the user consented to continue ("Is now a good time?").
    consent_to_continue: YesNoUnknown = "unknown"

    # True once post-verification disclosures have been delivered.
    disclosure_delivered: bool = False

    # True if a required acknowledgement (if any) has been obtained.
    mini_miranda_acknowledged: bool = False

    # Count of negotiation proposals made by the assistant.
    negotiation_proposals_count: int = 0

    # Count of reconduction (callback / not-a-good-time) attempts.
    reconduction_attempts: int = 0

    # Count of consecutive low-confidence clarification prompts.
    clarification_attempts: int = 0

    # Most recent finalized user transcript text (may be empty).
    last_user_utterance: str = ""

    # Last assistant question asked (for loop prevention; may be empty).
    last_assistant_question: str = ""

    # Last assistant intent (for context tracking; may be empty).
    last_assistant_intent: str = ""

    # Best-effort user sentiment label.
    user_sentiment: UserSentiment = "neutral"

    # True if the recipient indicated they are not the intended person.
    wrong_party_indicated: bool = False

    # True if voicemail was detected by the host system.
    voicemail_detected: bool = False

    # True if the user disputes the obligation.
    dispute_flag: bool = False

    # True if hardship/vulnerability cues are present.
    hardship_flag: bool = False

    # True if the user explicitly requested cease contact.
    cease_contact_requested: bool = False

    # Promise-to-pay details (post-verification only).
    promise_to_pay: PromiseToPay = Field(default_factory=PromiseToPay)

    # Callback scheduling details.
    callback: Callback = Field(default_factory=Callback)

    # Escalation reason code or short label (set when escalation_flag is true).
    escalation_reason: Optional[str] = None

    # End reason code/label (set only when phase == "ended").
    end_reason: Optional[str] = None

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from .intent_classifier import (
    IntentClassification,
    classify_utterance,
    is_low_confidence_unknown,
)
from .state import CallState
from .tools.date_normalizer import _last_day_of_month, normalize_datetime_local

TurnEventType = Literal["user_utterance", "silence", "system_event"]

@dataclass(frozen=True)
class TurnEvent:
    event_type: TurnEventType
    transcript: str | None
    timestamp_utc: str
    current_local_date: str
    current_local_time: str
    timezone: str
    language: str


def start_call(*, call_state: CallState, party_profile: dict[str, Any]) -> dict[str, Any]:
    """
    Produce the initial outbound prompt.
    Constraint: No debt/company mention yet.
    """
    new_state = call_state.model_copy(deep=True)
    new_state.turn_count += 1

    target_name = party_profile.get("target_name", "the account holder")
    assistant_text = f"Hello, I'm looking for {target_name}. Is this them?"
    return _wrap_response(assistant_text, "request_target", [], new_state)


def handle_turn(
    *,
    turn_event: TurnEvent,
    call_state: CallState,
    party_profile: dict[str, Any],
    account_context: dict[str, Any],
    policy_config: dict[str, Any],
) -> dict[str, Any]:
    """
    Refactored agent to enforce End of Month payment policy and strict identity verification.
    """
    new_state = call_state.model_copy(deep=True)

    if new_state.phase == "ended":
        return {
            "assistant_text": "This call is already closed. Goodbye.",
            "assistant_intent": "already_closed",
            "actions": [],
            "call_state": new_state,
            "nlu": {"primary_intent": "none", "confidence": 1.0, "scores": {}, "matched_intents": []},
        }

    new_state.turn_count += 1

    # End call if max turns reached
    max_total_turns = _get(policy_config, ("limits", "max_total_turns"), 25)
    if new_state.turn_count >= max_total_turns and new_state.phase != "ended":
        return _end_with_limit(new_state, policy_config)

    # Handle silence
    transcript = (turn_event.transcript or "").strip()
    if turn_event.event_type == "silence" or transcript == "":
        response = _handle_silence(turn_event, new_state, policy_config)
        response["nlu"] = {"primary_intent": "silence", "confidence": 1.0, "scores": {}, "matched_intents": ["silence"]}
        return response

    new_state.silence_count = 0
    nlu = classify_utterance(transcript)
    if not is_low_confidence_unknown(nlu):
        new_state.clarification_attempts = 0

    # Universal guards
    if nlu.matched("stop_request"):
        new_state.cease_contact_requested = True
        new_state.phase = "ended"
        new_state.end_reason = "cease_contact"
        response = _wrap_response(
            assistant_text="Understood, I will update our records to stop contacting you. Goodbye.",
            assistant_intent="close",
            actions=[
                {"action": "mark_do_not_contact", "scope": "account", "reason": "user_requested_cease_contact"},
                {"action": "set_outcome", "outcome_code": "cease_contact"},
                {"action": "end_call", "reason": "cease_contact"},
            ],
            call_state=new_state,
        )
        response["nlu"] = nlu.to_dict()
        return response

    if nlu.matched("goodbye"):
        response = _close_call(new_state, "Understood, thanks for your time. Goodbye.", "user_ended")
        response["nlu"] = nlu.to_dict()
        return response

    if nlu.matched("human_handoff"):
        new_state.escalation_flag = True
        new_state.escalation_reason = "user_requested_human"
        response = _escalate_and_end(new_state, policy_config, party_profile, account_context)
        response["nlu"] = nlu.to_dict()
        return response

    # Phase Routing
    if new_state.phase == "pre_verification":
        response = _handle_pre_verification(turn_event, new_state, party_profile, account_context, policy_config, transcript, nlu)
        response["nlu"] = nlu.to_dict()
        return response

    if new_state.phase == "verification":
        response = _handle_verification(turn_event, new_state, party_profile, account_context, policy_config, transcript, nlu)
        response["nlu"] = nlu.to_dict()
        return response

    if new_state.phase == "post_verification":
        response = _handle_negotiation(turn_event, new_state, party_profile, account_context, policy_config, transcript, nlu)
        response["nlu"] = nlu.to_dict()
        return response

    # Default close
    response = _close_call(new_state, "Thanks for your time. Goodbye.", "closed")
    response["nlu"] = nlu.to_dict()
    return response


def _handle_pre_verification(turn_event, state, party_profile, account_context, policy_config, transcript, nlu: IntentClassification):
    """Ensure no sensitive info is leaked until target is confirmed."""
    target_name = party_profile.get("target_name", "the account holder")

    if nlu.matched("wrong_party"):
        state.wrong_party_indicated = True
        state.target_reached = "no"
        return _close_call(state, "My apologies, I must have the wrong number. I'll update my records.", "wrong_party")

    if nlu.matched("identity_question"):
        # Policy: Introduce role but do not mention company or debt yet
        assistant_text = f"I am an automated assistant calling regarding a personal business matter for {target_name}. Is this them?"
        return _wrap_response(assistant_text, "request_target", [], state)

    if nlu.matched("affirmation"):
        state.target_reached = "yes"
        state.phase = "verification"
        return _ask_verification_question(state, policy_config, user_text=transcript)

    if is_low_confidence_unknown(nlu):
        return _handle_low_confidence(state, phase="pre_verification", party_profile=party_profile, policy_config=policy_config, account_context=account_context)

    # Re-prompt for target
    assistant_text = f"I'm trying to reach {target_name}. Is that you?"
    return _wrap_response(assistant_text, "request_target", [], state)


def _handle_verification(turn_event, state, party_profile, account_context, policy_config, transcript, nlu: IntentClassification):
    """Validate identity using ZIP or Name."""
    expected_zip = str(account_context.get("expected_zip", "")).strip()

    if nlu.matched("wrong_party"):
        state.wrong_party_indicated = True
        state.target_reached = "no"
        return _close_call(state, "My apologies, I must have the wrong number. I'll update my records.", "wrong_party")

    # Check for discomfort or negation (refusal to verify)
    if nlu.matched("uncomfortable") or nlu.matched("negation"):
        state.reconduction_attempts += 1
        if state.reconduction_attempts <= 2:
            assistant_text = (
                "I understand your privacy concern, and I can only discuss this with the account holder. "
                "Would it be better if I call you back at another time?"
            )
            return _wrap_response(assistant_text, "negotiate", [], state)
        return _close_call(state, "Since we are unable to verify your identity, I'll have to end the call now. Goodbye.", "verification_refused")

    if nlu.matched("identity_question"):
        assistant_text = (
            "To protect your privacy, I need to verify your identity before sharing details. "
            "Please confirm your 5-digit ZIP code."
        )
        return _wrap_response(assistant_text, "verify_identity", [], state)

    # Process ZIP verification
    provided_zip = _extract_zip(transcript)
    if provided_zip:
        if provided_zip == expected_zip:
            state.right_party_verified = True
            state.phase = "post_verification"
            return _deliver_disclosure_and_start_negotiation(state, policy_config, account_context)
        state.verification_attempts += 1
        if state.verification_attempts >= 3:
            return _close_call(
                state,
                "I'm sorry, that doesn't match our records, so I must end the call for security. Goodbye.",
                "verification_failed",
            )
        assistant_text = "I'm sorry, that ZIP code doesn't match our records. Could you please try again?"
        return _wrap_response(assistant_text, "verify_identity", [], state)

    if is_low_confidence_unknown(nlu):
        return _handle_low_confidence(
            state,
            phase="verification",
            party_profile=party_profile,
            policy_config=policy_config,
            account_context=account_context,
        )

    # If they provided some text but we couldn't find a 5-digit ZIP, count it as an attempt
    if transcript:
        state.verification_attempts += 1
        if state.verification_attempts >= 3:
            return _close_call(state, "I'm unable to verify your identity at this time. Goodbye.", "verification_failed")

    assistant_text = "To proceed, please tell me your 5-digit ZIP code clearly."
    return _wrap_response(assistant_text, "verify_identity", [], state)


def _handle_negotiation(turn_event, state, party_profile, account_context, policy_config, transcript, nlu: IntentClassification):
    """Enforce End of Month payment policy and handle evasive/refusal responses."""
    amount = _get_amount_due(account_context)
    last_q = (state.last_assistant_question or "").lower()

    # 1. Dispute always escalates immediately.
    if nlu.matched("dispute"):
        state.dispute_flag = True
        state.escalation_reason = "dispute"
        return _escalate_and_end(state, policy_config, party_profile, account_context)

    # 2. Confirmation of a previously proposed payment date/amount.
    if state.last_assistant_intent == "confirm_payment_date" and state.last_proposed_payment_date:
        if nlu.matched("affirmation") and not nlu.matched("negation") and not nlu.matched("refusal"):
            status = _validate_ptp_date(state.last_proposed_payment_date, turn_event.current_local_date)
            if status == "valid":
                confirmed_amount = state.last_proposed_payment_amount or amount
                return _confirm_ptp(state, state.last_proposed_payment_date, confirmed_amount)
            state.last_proposed_payment_date = None
            state.last_proposed_payment_amount = None
            last_day = _get_last_day_of_month_str(turn_event.current_local_date)
            if status == "in_past":
                assistant_text = f"That date has already passed. What date between today and {last_day} works for you?"
            else:
                assistant_text = f"Our policy requires a commitment by the end of this month. What date between today and {last_day} works for you?"
            return _wrap_response(assistant_text, "negotiate", [], state)
        if nlu.matched("negation") and not nlu.matched("refusal"):
            state.last_proposed_payment_date = None
            state.last_proposed_payment_amount = None
            assistant_text = "No problem. What exact date before month end works for you?"
            return _wrap_response(assistant_text, "negotiate", [], state)

    # 3. Refusal must be checked before deliver_disclosure affirmation handling.
    if nlu.matched("refusal"):
        state.negotiation_proposals_count += 1
        if state.negotiation_proposals_count >= 2:
            state.escalation_reason = "hard_refusal"
            return _escalate_and_end(state, policy_config, party_profile, account_context)
        assistant_text = (
            f"I understand things can be tight with a ${amount} balance. "
            "Is there a partial amount you can handle before the end of the month?"
        )
        return _wrap_response(assistant_text, "negotiate", [], state)

    # 4. Extract and validate proposed date before busy and deliver_disclosure affirmation handling.
    normalized = normalize_datetime_local(
        transcript,
        current_local_date=turn_event.current_local_date,
        current_local_time=turn_event.current_local_time,
        timezone=turn_event.timezone,
    )

    if normalized.get("ok") and normalized.get("date"):
        proposed_date = normalized["date"]
        date_status = _validate_ptp_date(proposed_date, turn_event.current_local_date)
        if date_status == "in_past":
            last_day = _get_last_day_of_month_str(turn_event.current_local_date)
            assistant_text = f"That date has already passed. What date between today and {last_day} works for you?"
            return _wrap_response(assistant_text, "negotiate", [], state)
        if date_status == "after_month_end":
            last_day = _get_last_day_of_month_str(turn_event.current_local_date)
            assistant_text = f"Our policy requires a commitment by the end of this month. What date between today and {last_day} works for you?"
            return _wrap_response(assistant_text, "negotiate", [], state)

        if normalized.get("needs_confirmation"):
            state.last_proposed_payment_date = proposed_date
            state.last_proposed_payment_amount = amount
            friendly_date = _format_iso_date_for_voice(proposed_date)
            assistant_text = f"Just to confirm, do you mean {friendly_date}?"
            return _wrap_response(assistant_text, "confirm_payment_date", [], state)

        return _confirm_ptp(state, proposed_date, amount)

    # 5. Direct yes/no response to the disclosure question ("Can you pay ... today?").
    if state.last_assistant_intent == "deliver_disclosure":
        if nlu.matched("negation"):
            assistant_text = "I understand. What date before the end of the month would work for you?"
            return _wrap_response(assistant_text, "negotiate", [], state)
        if nlu.matched("affirmation") and not nlu.matched("negation") and not nlu.matched("refusal"):
            return _confirm_ptp(state, turn_event.current_local_date, amount)

    # 6. Uncertain and busy (only reached when no payment date was extracted).
    if nlu.matched("uncertain"):
        assistant_text = "I can wait while you check your calendar. Or, would you prefer if I suggest a date near the end of the month?"
        return _wrap_response(assistant_text, "negotiate", [], state)

    if nlu.matched("busy"):
        return _close_call(state, "I understand, we will try you again at a better time. Goodbye.", "busy")

    # 7. Response to any "can you pay/take care of this balance today?" style prompt.
    if _is_today_payment_prompt(last_q):
        if _looks_like_affirmative_today_response(transcript):
            return _confirm_ptp(state, turn_event.current_local_date, amount)
        if nlu.matched("negation"):
            assistant_text = "I understand. What date before the end of the month would work for you?"
            return _wrap_response(assistant_text, "negotiate", [], state)
        if nlu.matched("affirmation") and not nlu.matched("negation") and not nlu.matched("refusal"):
            return _confirm_ptp(state, turn_event.current_local_date, amount)

    # If we asked for an exact date and received only generic confirmation, ask
    # for a concrete date format instead of repeating the same prompt verbatim.
    if _is_exact_date_request_prompt(last_q) and nlu.matched("affirmation"):
        assistant_text = "Thanks. Please tell me the exact payment date, for example February 20."
        return _wrap_response(assistant_text, "negotiate", [], state)

    # 8. If the user says "No" or refuses again, propose a dynamic partial payment offer.
    if nlu.matched("negation") or nlu.matched("refusal"):
        state.negotiation_proposals_count += 1
        if state.negotiation_proposals_count >= 2:
            state.escalation_reason = "multiple_refusals"
            return _escalate_and_end(state, policy_config, party_profile, account_context)

        partial_amount, offer_date_iso = _build_partial_offer(amount, turn_event.current_local_date)
        state.last_proposed_payment_date = offer_date_iso
        state.last_proposed_payment_amount = partial_amount
        offer_date = date.fromisoformat(offer_date_iso)
        current_date = date.fromisoformat(turn_event.current_local_date)
        date_phrase = "today" if offer_date == current_date else f"by {offer_date.strftime('%B %d')}"
        assistant_text = (
            f"I hear you. If a full payment isn't possible, could you do a partial payment of ${partial_amount} {date_phrase}?"
        )
        return _wrap_response(assistant_text, "confirm_payment_date", [], state)

    # Initial negotiation prompt if just verified
    if state.last_assistant_intent == "deliver_disclosure":
        assistant_text = f"Great. Can you take care of the ${amount} balance today?"
        return _wrap_response(assistant_text, "negotiate", [], state)

    if is_low_confidence_unknown(nlu):
        return _handle_low_confidence(
            state,
            phase="post_verification",
            party_profile=party_profile,
            policy_config=policy_config,
            account_context=account_context,
        )

    # Default negotiation fallback
    assistant_text = f"Can you find a day before the end of the month to settle this ${amount}?"
    return _wrap_response(assistant_text, "negotiate", [], state)


# --- Helpers ---

def _deliver_disclosure_and_start_negotiation(state, policy_config, account_context):
    amount = _get_amount_due(account_context)
    disclosure = _get(policy_config, ("disclosures", "post_verification_disclosure_text"), "").strip()
    disclosure_parts = [p.strip() for p in disclosure.split(".") if p.strip()]
    if len(disclosure_parts) >= 2:
        disclosure_single_sentence = f"{disclosure_parts[0]}; {disclosure_parts[1]}"
    elif disclosure_parts:
        disclosure_single_sentence = disclosure_parts[0]
    else:
        disclosure_single_sentence = (
            "This is Northstar Recovery; this is an attempt to collect a debt, "
            "and any information obtained will be used for that purpose"
        )

    assistant_text = (
        f"{disclosure_single_sentence}. Can you pay the ${amount} balance today?"
    )
    state.disclosure_delivered = True
    return _wrap_response(assistant_text, "deliver_disclosure", [], state)

def _confirm_ptp(state, date_str, amount):
    assistant_text = f"Perfect, I've noted your commitment for ${amount} on {date_str}. Thank you, and have a great day."
    state.promise_to_pay.date = date_str
    state.promise_to_pay.amount = amount
    state.promise_to_pay.confirmed = True
    state.last_proposed_payment_date = date_str
    state.last_proposed_payment_amount = amount
    actions = [
        {"action": "set_outcome", "outcome_code": "ptp_set"},
        {"action": "create_promise_to_pay", "date": date_str, "amount": amount},
        {"action": "end_call", "reason": "ptp_set"}
    ]
    state.phase = "ended"
    state.end_reason = "ptp_set"
    return _wrap_response(assistant_text, "close", actions, state)

def _validate_ptp_date(proposed_iso: str, current_iso: str) -> Literal["valid", "in_past", "after_month_end"]:
    proposed = date.fromisoformat(proposed_iso)
    current = date.fromisoformat(current_iso)
    last_day = _last_day_of_month(current)
    if proposed < current:
        return "in_past"
    if proposed > last_day:
        return "after_month_end"
    return "valid"

def _build_partial_offer(amount_due: str, current_local_date: str) -> tuple[str, str]:
    balance = Decimal(amount_due).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if balance <= Decimal("1.00"):
        partial = balance
    else:
        partial = (balance / Decimal("2")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if partial >= balance:
            partial = (balance - Decimal("0.01")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    current_date = date.fromisoformat(current_local_date)
    last_day = _last_day_of_month(current_date)
    offer_date = min(current_date + timedelta(days=5), last_day)
    return f"{partial:.2f}", offer_date.isoformat()

def _get_last_day_of_month_str(current_iso):
    dt = datetime.fromisoformat(current_iso).date()
    return _last_day_of_month(dt).strftime("%B %d")

def _close_call(state, text, outcome):
    state.phase = "ended"
    state.end_reason = outcome
    return _wrap_response(
        assistant_text=text,
        assistant_intent="close",
        actions=[{"action": "set_outcome", "outcome_code": outcome}, {"action": "end_call", "reason": outcome}],
        call_state=state
    )

def _handle_low_confidence(
    state: CallState,
    *,
    phase: str,
    party_profile: dict[str, Any],
    policy_config: dict[str, Any],
    account_context: dict[str, Any],
) -> dict[str, Any]:
    state.clarification_attempts += 1
    target_name = party_profile.get("target_name", "the account holder")

    if state.clarification_attempts <= 1:
        if phase == "pre_verification":
            assistant_text = f"Sorry, I didn't catch that. Are you {target_name}?"
            intent = "request_target"
        elif phase == "verification":
            assistant_text = "Sorry, I didn't catch that. Please confirm your 5-digit ZIP code."
            intent = "verify_identity"
        else:
            assistant_text = "Sorry, I didn't catch that. Could you repeat the payment date that works for you?"
            intent = "negotiate"
        return _wrap_response(assistant_text, intent, [], state)

    state.escalation_flag = True
    state.escalation_reason = "low_confidence"
    return _escalate_and_end(state, policy_config, party_profile, account_context)

def _extract_zip(text: str) -> str | None:
    # Direct 5-digit ZIP.
    match = re.search(r"\b(\d{5})\b", text)
    if match:
        return match.group(1)

    # Handle split numeric forms like "78 and 701" -> "78701".
    numeric_digits = re.findall(r"\d", text)
    if len(numeric_digits) >= 5:
        return "".join(numeric_digits[:5])

    # Handle spoken forms like "seven eight seven zero one".
    spoken_digit_map = {
        "zero": "0",
        "oh": "0",
        "o": "0",
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
    }
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    spoken_digits: list[str] = []
    for token in tokens:
        if token in spoken_digit_map:
            spoken_digits.append(spoken_digit_map[token])
        elif token.isdigit():
            spoken_digits.extend(list(token))

    if len(spoken_digits) >= 5:
        return "".join(spoken_digits[:5])

    # Handle full number-word forms like "seventy eight thousand and seven hundred and one".
    word_number = _extract_number_from_words(text)
    if word_number is not None and 10000 <= word_number <= 99999:
        return str(word_number)

    return None


def _is_today_payment_prompt(last_question_text: str) -> bool:
    text = (last_question_text or "").lower()
    if "today" not in text:
        return False
    return any(marker in text for marker in ["take care", "pay", "balance"])


def _is_exact_date_request_prompt(last_question_text: str) -> bool:
    text = (last_question_text or "").lower()
    return "find a day before the end of the month" in text or "what date before the end of the month" in text


_NEGATION_TODAY_RE = re.compile(r"\b(no|not|can't|cannot|don't|do not|won't|wont)\b", re.I)
_AFFIRMATIVE_TODAY_RE = re.compile(
    r"\b(yes|yeah|yep|sure|i can(?!'t\b|\s+not\b)|can do|take care|pay today)\b",
    re.I,
)


def _looks_like_affirmative_today_response(user_text: str) -> bool:
    text = (user_text or "").replace("\u2019", "'").strip().lower()
    if not text:
        return False
    if _NEGATION_TODAY_RE.search(text):
        return False
    return bool(_AFFIRMATIVE_TODAY_RE.search(text))


def _extract_number_from_words(text: str) -> int | None:
    units = {
        "zero": 0,
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
    }
    tens = {
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "fifty": 50,
        "sixty": 60,
        "seventy": 70,
        "eighty": 80,
        "ninety": 90,
    }

    tokens = re.findall(r"[a-z]+", (text or "").lower())
    if not tokens:
        return None

    total = 0
    current = 0
    saw_number_token = False
    for token in tokens:
        if token in units:
            current += units[token]
            saw_number_token = True
        elif token in tens:
            current += tens[token]
            saw_number_token = True
        elif token == "hundred":
            current = max(1, current) * 100
            saw_number_token = True
        elif token == "thousand":
            total += max(1, current) * 1000
            current = 0
            saw_number_token = True
        elif token == "and":
            continue
        elif saw_number_token:
            break
        else:
            # Ignore leading non-number tokens.
            continue

    if not saw_number_token:
        return None
    return total + current

def _get_amount_due(context):
    val = context.get("amount_due", "0.00")
    return f"{float(val):.2f}"

def _ask_verification_question(state, policy_config, *, user_text: str = ""):
    last_user_text = (user_text or "").lower()
    if "what" in last_user_text or "why" in last_user_text or "who" in last_user_text:
        assistant_text = "I can certainly explain that, but first, to protect your privacy, I need to verify I'm speaking with the right person. Could you please confirm your 5-digit ZIP code?"
    else:
        assistant_text = "To protect your privacy, please confirm your 5-digit ZIP code."

    return _wrap_response(assistant_text, "verify_identity", [], state)

def _handle_silence(event, state, config):
    state.silence_count += 1
    if state.silence_count >= 3:
        return _close_call(state, "Since I haven't heard from you, I'll end the call for now. Goodbye.", "silence_timeout")
    assistant_text = "Are you still there? I didn't catch that."
    return _wrap_response(assistant_text, "handle_silence", [], state)

def _end_with_limit(state, config):
    return _close_call(state, "Thank you for your time. Goodbye.", "max_turns")

def _escalate_and_end(state, config, profile, context):
    state.escalation_flag = True
    outcome = f"escalated_{state.escalation_reason or 'unknown'}"
    state.phase = "ended"
    state.end_reason = outcome
    return _wrap_response(
        "I'll connect you with a specialist who can help further. Please hold.",
        "escalate",
        [
            {"action": "set_outcome", "outcome_code": outcome},
            {"action": "escalate_to_human", "reason": state.escalation_reason},
            {"action": "end_call", "reason": outcome},
        ],
        state,
    )

def _get(d, path, default=None):
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return default
        d = d[key]
    return d

def _wrap_response(assistant_text: str, assistant_intent: str, actions: list[dict[str, Any]], call_state: CallState) -> dict[str, Any]:
    """Helper to ensure the last_assistant_intent is consistently stored in state."""
    constrained_text = _enforce_voice_first(assistant_text)
    call_state.last_assistant_question = constrained_text if "?" in constrained_text else ""
    call_state.last_assistant_intent = assistant_intent
    return {
        "assistant_text": constrained_text,
        "assistant_intent": assistant_intent,
        "actions": actions,
        "call_state": call_state,
    }


def _format_iso_date_for_voice(iso_date: str) -> str:
    dt = datetime.fromisoformat(iso_date).date()
    return dt.strftime("%A, %B %d")


def _enforce_voice_first(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""

    # Split on sentence punctuation followed by whitespace. This avoids
    # breaking decimal values like 240.00 into separate "sentences".
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    if not sentences:
        sentences = [cleaned]

    if len(sentences) > 2:
        first_two = sentences[:2]
        if not any("?" in s for s in first_two):
            later_question = next((s for s in sentences[2:] if "?" in s), None)
            limited = [sentences[0], later_question] if later_question else first_two
        else:
            limited = first_two
    else:
        limited = sentences

    question_seen = False
    normalized: list[str] = []

    for sentence in limited:
        if "?" in sentence:
            if question_seen:
                sentence = sentence.replace("?", ".")
            else:
                first_q = sentence.find("?")
                sentence = sentence[: first_q + 1] + sentence[first_q + 1 :].replace("?", "")
                question_seen = True
        normalized.append(sentence.strip())

    constrained = " ".join(normalized).strip()
    if constrained and constrained[-1] not in ".!?":
        constrained += "."
    return constrained

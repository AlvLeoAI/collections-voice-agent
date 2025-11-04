#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from dotenv import load_dotenv

from zoneinfo import ZoneInfo

# Ensure project root is on sys.path when running from ./scripts
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.outbound_voice_agent import CallState, handle_turn, start_call  # noqa: E402
from src.outbound_voice_agent.agent import TurnEvent  # noqa: E402
from src.voice_handler import VoiceHandler # noqa: E402

# Load environment variables
load_dotenv()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a minimal outbound collections agent demo (no telephony).")
    parser.add_argument("--scenario", type=str, default=None, help="Path to a scenario JSON file to replay.")
    parser.add_argument("--timezone", type=str, default="America/Chicago")
    parser.add_argument("--language", type=str, default="en-US")
    args = parser.parse_args()

    party_profile, account_context, policy_config = default_context()

    tz = ZoneInfo(args.timezone)
    call_state = CallState()
    voice_handler = VoiceHandler()

    # Initial outbound prompt
    initial = start_call(call_state=call_state, party_profile=party_profile)
    call_state = initial["call_state"]
    print_output(None, initial, call_state, voice_handler)

    if args.scenario:
        scenario_path = Path(args.scenario)
        scenario = load_scenario(scenario_path)
        return run_scenario(
            scenario,
            call_state=call_state,
            party_profile=party_profile,
            account_context=account_context,
            policy_config=policy_config,
            default_timezone=args.timezone,
            default_language=args.language,
            voice_handler=voice_handler,
        )

    return run_interactive(
        call_state=call_state,
        party_profile=party_profile,
        account_context=account_context,
        policy_config=policy_config,
        tz=tz,
        language=args.language,
        voice_handler=voice_handler,
    )


def run_interactive(
    *,
    call_state: CallState,
    party_profile: Dict[str, Any],
    account_context: Dict[str, Any],
    policy_config: Dict[str, Any],
    tz: ZoneInfo,
    language: str,
    voice_handler: VoiceHandler,
) -> int:
    print("\nType your responses. Press Enter on a blank line to simulate silence. Type 'quit' to exit.\n")
    while call_state.phase != "ended":
        user_text = input("You: ").strip()
        if user_text.lower() in {"quit", "exit"}:
            break

        now_local = datetime.now(tz)
        now_utc = now_local.astimezone(timezone.utc)
        event = TurnEvent(
            event_type="silence" if user_text == "" else "user_utterance",
            transcript=None if user_text == "" else user_text,
            timestamp_utc=now_utc.isoformat(),
            current_local_date=now_local.date().isoformat(),
            current_local_time=now_local.strftime("%H:%M"),
            timezone=str(tz.key),
            language=language,
        )

        result = handle_turn(
            turn_event=event,
            call_state=call_state,
            party_profile=party_profile,
            account_context=account_context,
            policy_config=policy_config,
        )
        call_state = result["call_state"]
        print_output(user_text if user_text != "" else None, result, call_state, voice_handler)

    return 0


def run_scenario(
    scenario: Dict[str, Any],
    *,
    call_state: CallState,
    party_profile: Dict[str, Any],
    account_context: Dict[str, Any],
    policy_config: Dict[str, Any],
    default_timezone: str,
    default_language: str,
    voice_handler: VoiceHandler,
) -> int:
    tz_name = str(scenario.get("timezone") or default_timezone)
    lang = str(scenario.get("language") or default_language)
    tz = ZoneInfo(tz_name)

    start_local_datetime = scenario.get("start_local_datetime")
    if start_local_datetime:
        sim_local = datetime.fromisoformat(start_local_datetime).replace(tzinfo=tz)
    else:
        sim_local = datetime.now(tz).replace(second=0, microsecond=0)

    events = scenario.get("events")
    if not isinstance(events, list):
        raise ValueError("Scenario JSON must include an 'events' array.")

    print(f"\n--- Scenario: {scenario.get('name','(unnamed)')} ---\n")

    for idx, raw in enumerate(events, start=1):
        event_type = raw.get("event_type")
        if event_type not in {"user_utterance", "silence"}:
            raise ValueError(f"Invalid event_type at index {idx}: {event_type}")

        transcript = raw.get("transcript") if event_type == "user_utterance" else None
        now_utc = sim_local.astimezone(timezone.utc)
        event = TurnEvent(
            event_type=event_type,
            transcript=transcript,
            timestamp_utc=now_utc.isoformat(),
            current_local_date=sim_local.date().isoformat(),
            current_local_time=sim_local.strftime("%H:%M"),
            timezone=tz_name,
            language=lang,
        )

        if event_type == "user_utterance":
            print(f"User: {transcript}")
        else:
            print("User: [silence]")

        result = handle_turn(
            turn_event=event,
            call_state=call_state,
            party_profile=party_profile,
            account_context=account_context,
            policy_config=policy_config,
        )
        call_state = result["call_state"]
        print_output(transcript, result, call_state, voice_handler)

        if call_state.phase == "ended":
            break

        sim_local = sim_local + timedelta(seconds=25)

    return 0


def print_output(user_text: Optional[str], result: Dict[str, Any], call_state: CallState, voice_handler: VoiceHandler) -> None:
    assistant_text = result.get("assistant_text", "")
    intent = result.get("assistant_intent", "")
    actions = result.get("actions", [])
    nlu = result.get("nlu")

    print(f"Assistant says: {assistant_text}")
    
    # Live voice streaming
    if assistant_text:
        voice_handler.text_to_speech_stream(assistant_text)

    print(f"Intent: {intent}")
    if nlu:
        print(f"NLU: intent={nlu.get('primary_intent')}, confidence={nlu.get('confidence')}")

    if actions:
        print("Actions:")
        print(json.dumps(actions, indent=2))
    else:
        print("Actions: []")

    print(
        "State:",
        f"phase={call_state.phase},",
        f"turns={call_state.turn_count},",
        f"verification_attempts={call_state.verification_attempts},",
        f"silence_count={call_state.silence_count},",
        f"negotiation_proposals_count={call_state.negotiation_proposals_count}",
    )
    print("-" * 72)


def default_context() -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    party_profile = {
        "target_name": "Alex Morgan",
        "target_preferred_name": "Alex",
        "callback_number": "+1 (555) 010-2000",
    }

    # Demo-only context used for deterministic verification and post-verification negotiation.
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
                "This is Northstar Recovery. This is an attempt to collect a debt, and any information obtained will be used for that purpose."
            ),
            "voicemail_message_text": "Hi, this is Northstar Recovery. Please call us back at +1 (555) 010-2000.",
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
        "handoff": {"human_escalation_available": True, "escalation_reason_codes": ["dispute", "verification_failed", "user_requested_human"]},
    }

    return party_profile, account_context, policy_config


def load_scenario(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        content = json.load(f)
    if isinstance(content, list):
        return {"name": path.stem, "events": content}
    if not isinstance(content, dict):
        raise ValueError("Scenario file must be a JSON object or an array of events.")
    return content


if __name__ == "__main__":
    raise SystemExit(main())

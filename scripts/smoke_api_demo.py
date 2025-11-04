#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# Ensure project root is on sys.path when running from ./scripts
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def default_context() -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
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


def scenario_events(name: str) -> List[Dict[str, Any]]:
    scenarios: Dict[str, List[Dict[str, Any]]] = {
        "happy_path": [
            {"event_type": "user_utterance", "transcript": "This is Alex Morgan."},
            {"event_type": "user_utterance", "transcript": "78701."},
            {"event_type": "user_utterance", "transcript": "End of month."},
        ],
        "wrong_party": [
            {"event_type": "user_utterance", "transcript": "You have the wrong number. Alex doesn't live here."},
        ],
        "dispute": [
            {"event_type": "user_utterance", "transcript": "This is Alex Morgan."},
            {"event_type": "user_utterance", "transcript": "78701."},
            {"event_type": "user_utterance", "transcript": "I don't owe this."},
        ],
    }
    return scenarios[name]


def build_turn_event(event: Dict[str, Any], tz_name: str, language: str) -> Dict[str, Any]:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    now_utc = now_local.astimezone(timezone.utc)
    return {
        "event_type": event["event_type"],
        "transcript": event.get("transcript"),
        "timestamp_utc": now_utc.isoformat(),
        "current_local_date": now_local.date().isoformat(),
        "current_local_time": now_local.strftime("%H:%M"),
        "timezone": tz_name,
        "language": language,
    }


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Run a one-command FastAPI smoke demo.")
    parser.add_argument("--mode", choices=["inprocess", "http"], default="inprocess")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="FastAPI base URL (http mode only)")
    parser.add_argument("--scenario", choices=["happy_path", "wrong_party", "dispute"], default="happy_path")
    parser.add_argument("--timezone", default="America/Chicago")
    parser.add_argument("--language", default="en-US")
    parser.add_argument("--print-summary-json", action="store_true")
    args = parser.parse_args()

    party_profile, account_context, policy_config = default_context()
    events = scenario_events(args.scenario)

    if args.mode == "inprocess":
        from src.api.server import (
            StartCallRequest,
            TurnRequest,
            api_get_call_summary,
            api_handle_turn,
            api_start_call,
        )

        start_data = asyncio.run(api_start_call(StartCallRequest(party_profile=party_profile)))
        call_id = start_data["call_id"]
        call_state = start_data["call_state"]
        print(f"call_id={call_id}")
        print(f"assistant(start)={start_data['assistant_intent']} | {start_data['assistant_text']}")

        for idx, event in enumerate(events, start=1):
            payload = TurnRequest(
                call_id=call_id,
                turn_event=build_turn_event(event, args.timezone, args.language),
                call_state=call_state,
                party_profile=party_profile,
                account_context=account_context,
                policy_config=policy_config,
            )
            turn_data = asyncio.run(api_handle_turn(payload))
            call_state = turn_data["call_state"]
            print(f"assistant(turn {idx})={turn_data['assistant_intent']} | {turn_data['assistant_text']}")
            print(f"actions(turn {idx})={turn_data.get('actions', [])}")
            if call_state.get("phase") == "ended":
                break

        summary = asyncio.run(api_get_call_summary(call_id))
    else:
        with httpx.Client(timeout=30.0) as client:
            start_resp = client.post(f"{args.base_url}/call/start", json={"party_profile": party_profile})
            if start_resp.status_code != 200:
                print(f"ERROR /call/start [{start_resp.status_code}]: {start_resp.text}")
                return 1

            start_data = start_resp.json()
            call_id = start_data["call_id"]
            call_state = start_data["call_state"]
            print(f"call_id={call_id}")
            print(f"assistant(start)={start_data['assistant_intent']} | {start_data['assistant_text']}")

            for idx, event in enumerate(events, start=1):
                payload = {
                    "call_id": call_id,
                    "turn_event": build_turn_event(event, args.timezone, args.language),
                    "call_state": call_state,
                    "party_profile": party_profile,
                    "account_context": account_context,
                    "policy_config": policy_config,
                }
                turn_resp = client.post(f"{args.base_url}/call/turn", json=payload)
                if turn_resp.status_code != 200:
                    print(f"ERROR /call/turn [{turn_resp.status_code}] turn={idx}: {turn_resp.text}")
                    return 1

                turn_data = turn_resp.json()
                call_state = turn_data["call_state"]
                print(f"assistant(turn {idx})={turn_data['assistant_intent']} | {turn_data['assistant_text']}")
                print(f"actions(turn {idx})={turn_data.get('actions', [])}")
                if call_state.get("phase") == "ended":
                    break

            summary_resp = client.get(f"{args.base_url}/call/{call_id}")
            if summary_resp.status_code != 200:
                print(f"ERROR /call/{{call_id}} [{summary_resp.status_code}]: {summary_resp.text}")
                return 1
            summary = summary_resp.json()

    print(
        "summary:",
        f"status={summary.get('status')},",
        f"turns_count={summary.get('turns_count')},",
        f"final_outcome_code={summary.get('final_outcome_code')},",
        f"final_end_reason={summary.get('final_end_reason')}",
    )
    print(f"persisted_file=runtime/calls/{call_id}.json")

    if args.print_summary_json:
        print(json.dumps(summary, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

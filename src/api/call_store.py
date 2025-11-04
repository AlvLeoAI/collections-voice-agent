from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from src.outbound_voice_agent import CallState
from src.outbound_voice_agent.agent import TurnEvent


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_outcome_code(actions: List[Dict[str, Any]]) -> Optional[str]:
    for action in actions:
        if action.get("action") == "set_outcome":
            return action.get("outcome_code")
    return None


def _extract_end_reason(actions: List[Dict[str, Any]]) -> Optional[str]:
    for action in actions:
        if action.get("action") == "end_call":
            return action.get("reason")
    return None


class JsonCallStore:
    """Persist demo call turns and outcomes in local JSON files."""

    def __init__(self, root_dir: str | Path = "runtime/calls") -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def generate_call_id(self) -> str:
        return uuid.uuid4().hex

    def _path_for(self, call_id: str) -> Path:
        return self.root_dir / f"{call_id}.json"

    def create_call(
        self,
        *,
        call_id: str,
        assistant_intent: str,
        call_state: CallState,
    ) -> Dict[str, Any]:
        now = _utc_now_iso()
        record = {
            "call_id": call_id,
            "status": "active",
            "created_at_utc": now,
            "updated_at_utc": now,
            "turns": [
                {
                    "turn_index": 1,
                    "timestamp_utc": now,
                    "recorded_at_utc": now,
                    "event_type": "system_start",
                    "assistant_intent": assistant_intent,
                    "actions": [],
                }
            ],
            "final_outcome_code": None,
            "final_end_reason": None,
            "last_call_state": call_state.model_dump(mode="json"),
        }
        self._write_record(record)
        return record

    def append_turn(
        self,
        *,
        call_id: str,
        turn_event: TurnEvent,
        assistant_intent: str,
        actions: List[Dict[str, Any]],
        call_state: CallState,
        nlu: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            record = self._read_record(call_id)
            now = _utc_now_iso()
            next_index = len(record.get("turns", [])) + 1
            turn_row = {
                "turn_index": next_index,
                "timestamp_utc": turn_event.timestamp_utc,
                "recorded_at_utc": now,
                "event_type": turn_event.event_type,
                "user_transcript_present": bool((turn_event.transcript or "").strip()),
                "assistant_intent": assistant_intent,
                "actions": actions,
                "nlu_intent": (nlu or {}).get("primary_intent"),
                "nlu_confidence": (nlu or {}).get("confidence"),
            }
            record["turns"].append(turn_row)
            record["updated_at_utc"] = now
            record["last_call_state"] = call_state.model_dump(mode="json")

            if call_state.phase == "ended":
                record["status"] = "ended"
                outcome_code = _extract_outcome_code(actions)
                end_reason = call_state.end_reason or _extract_end_reason(actions)
                if outcome_code:
                    record["final_outcome_code"] = outcome_code
                elif end_reason and not record.get("final_outcome_code"):
                    # Fallback for flows that only emit end_call(reason=...).
                    record["final_outcome_code"] = end_reason
                if end_reason:
                    record["final_end_reason"] = end_reason

            self._write_record(record)
            return record

    def get_call(self, call_id: str) -> Dict[str, Any]:
        with self._lock:
            return self._read_record(call_id)

    def get_call_state(self, call_id: str) -> CallState:
        record = self.get_call(call_id)
        return CallState.model_validate(record.get("last_call_state", {}))

    def list_calls(self) -> List[Dict[str, Any]]:
        with self._lock:
            records: List[Dict[str, Any]] = []
            for path in sorted(self.root_dir.glob("*.json")):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        item = json.load(f)
                    if isinstance(item, dict):
                        records.append(item)
                except Exception:
                    # Keep metrics/report features robust to partial/corrupt files.
                    continue
            return records

    def summarize_call(self, call_id: str) -> Dict[str, Any]:
        record = self.get_call(call_id)
        turns = record.get("turns", [])
        last_turn = turns[-1] if turns else {}
        return {
            "call_id": record.get("call_id"),
            "status": record.get("status"),
            "created_at_utc": record.get("created_at_utc"),
            "updated_at_utc": record.get("updated_at_utc"),
            "turns_count": len(turns),
            "last_assistant_intent": last_turn.get("assistant_intent"),
            "final_outcome_code": record.get("final_outcome_code"),
            "final_end_reason": record.get("final_end_reason"),
            "last_call_state": record.get("last_call_state"),
            "turns": turns,
        }

    def _read_record(self, call_id: str) -> Dict[str, Any]:
        path = self._path_for(call_id)
        if not path.exists():
            raise FileNotFoundError(f"Unknown call_id: {call_id}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_record(self, record: Dict[str, Any]) -> None:
        path = self._path_for(str(record["call_id"]))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from src.api.storage_utils import atomic_write_json

_VALID_SUPPRESSION_FLAGS = {"dnc", "cease_contact", "legal_hold"}


def _default_suppressions() -> dict[str, bool]:
    return {"dnc": False, "cease_contact": False, "legal_hold": False}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _account_file_slug(account_ref: str) -> str:
    digest = hashlib.sha256(account_ref.encode("utf-8")).hexdigest()
    return digest[:32]


class JsonContactAttemptStore:
    """
    Per-account ledger for outbound contact decisions and attempts.

    Stores minimal, non-sensitive fields used for policy gating.
    """

    def __init__(self, root_dir: str | Path = "runtime/attempts") -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _path_for(self, account_ref: str) -> Path:
        return self.root_dir / f"{_account_file_slug(account_ref)}.json"

    def append_event(
        self,
        *,
        account_ref: str,
        decision_code: str,
        counts_toward_attempt: bool,
        job_id: str | None = None,
        call_id: str | None = None,
        recorded_at_utc: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            row = self._read_row_locked(account_ref)
            event = {
                "recorded_at_utc": recorded_at_utc or _utc_now_iso(),
                "job_id": job_id,
                "call_id": call_id,
                "decision_code": decision_code,
                "counts_toward_attempt": bool(counts_toward_attempt),
            }
            row["events"].append(event)
            self._write_row_locked(account_ref, row)
            return event

    def set_suppression(
        self,
        *,
        account_ref: str,
        flag: str,
        reason: str,
        call_id: str | None = None,
        job_id: str | None = None,
        recorded_at_utc: str | None = None,
    ) -> dict[str, Any]:
        if flag not in _VALID_SUPPRESSION_FLAGS:
            raise ValueError(f"Unsupported suppression flag: {flag!r}")
        with self._lock:
            row = self._read_row_locked(account_ref)
            row["suppressions"][flag] = True
            event = {
                "recorded_at_utc": recorded_at_utc or _utc_now_iso(),
                "job_id": job_id,
                "call_id": call_id,
                "decision_code": f"suppression_recorded_{flag}",
                "counts_toward_attempt": False,
                "reason": reason,
            }
            row["events"].append(event)
            self._write_row_locked(account_ref, row)
            return event

    def get_suppressions(self, account_ref: str) -> dict[str, bool]:
        with self._lock:
            row = self._read_row_locked(account_ref)
            suppressions = row.get("suppressions", {})
            return {
                "dnc": bool(suppressions.get("dnc", False)),
                "cease_contact": bool(suppressions.get("cease_contact", False)),
                "legal_hold": bool(suppressions.get("legal_hold", False)),
            }

    def list_events(self, account_ref: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._read_row_locked(account_ref).get("events", []))

    def list_recent_events(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            merged: list[dict[str, Any]] = []
            for path in sorted(self.root_dir.glob("*.json")):
                try:
                    with open(path, encoding="utf-8") as f:
                        row = json.load(f)
                except Exception:
                    continue
                account_ref = row.get("account_ref")
                for event in row.get("events", []):
                    if not isinstance(event, dict):
                        continue
                    merged.append(
                        {
                            "account_ref": account_ref,
                            "recorded_at_utc": event.get("recorded_at_utc"),
                            "job_id": event.get("job_id"),
                            "call_id": event.get("call_id"),
                            "decision_code": event.get("decision_code"),
                            "counts_toward_attempt": bool(event.get("counts_toward_attempt", False)),
                        }
                    )
            merged.sort(key=lambda x: str(x.get("recorded_at_utc", "")), reverse=True)
            if limit > 0:
                return merged[:limit]
            return merged

    def count_attempts_for_local_day(
        self,
        *,
        account_ref: str,
        timezone_name: str,
        local_day_iso: str,
    ) -> int:
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            raise ValueError("timezone_name must be a non-empty IANA timezone string.")
        tz = ZoneInfo(timezone_name)

        total = 0
        for event in self.list_events(account_ref):
            if not bool(event.get("counts_toward_attempt", False)):
                continue
            ts = event.get("recorded_at_utc")
            if not isinstance(ts, str):
                continue
            try:
                local_day = _parse_iso_utc(ts).astimezone(tz).date().isoformat()
            except Exception:
                continue
            if local_day == local_day_iso:
                total += 1
        return total

    def get_last_counted_attempt_at_utc(self, *, account_ref: str) -> str | None:
        latest: datetime | None = None
        for event in self.list_events(account_ref):
            if not bool(event.get("counts_toward_attempt", False)):
                continue
            ts = event.get("recorded_at_utc")
            if not isinstance(ts, str):
                continue
            try:
                parsed = _parse_iso_utc(ts)
            except Exception:
                continue
            if latest is None or parsed > latest:
                latest = parsed
        return latest.isoformat() if latest else None

    def _read_row_locked(self, account_ref: str) -> dict[str, Any]:
        path = self._path_for(account_ref)
        if not path.exists():
            return {"account_ref": account_ref, "events": [], "suppressions": _default_suppressions()}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"account_ref": account_ref, "events": [], "suppressions": _default_suppressions()}
        data.setdefault("account_ref", account_ref)
        events = data.get("events")
        if not isinstance(events, list):
            data["events"] = []
        suppressions = data.get("suppressions")
        if not isinstance(suppressions, dict):
            data["suppressions"] = _default_suppressions()
        else:
            for key in _VALID_SUPPRESSION_FLAGS:
                suppressions.setdefault(key, False)
        return data

    def _write_row_locked(self, account_ref: str, row: dict[str, Any]) -> None:
        path = self._path_for(account_ref)
        atomic_write_json(path, row)

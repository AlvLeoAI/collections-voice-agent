from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any, Dict, List, Optional, Tuple


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(value: Any, *, assume_utc_for_naive: bool) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        if not assume_utc_for_naive:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _turn_has_ptp_action(turn: Dict[str, Any]) -> bool:
    actions = turn.get("actions")
    if not isinstance(actions, list):
        return False

    for action in actions:
        if not isinstance(action, dict):
            continue
        if action.get("action") == "set_outcome" and action.get("outcome_code") == "ptp_set":
            return True
        if action.get("action") == "create_promise_to_pay":
            return True
    return False


def _extract_ptp_info(record: Dict[str, Any]) -> Tuple[bool, Optional[datetime]]:
    has_ptp = False
    ptp_timestamp: Optional[datetime] = None

    for turn in record.get("turns", []):
        if not isinstance(turn, dict) or not _turn_has_ptp_action(turn):
            continue

        has_ptp = True
        ptp_timestamp = _parse_iso_datetime(turn.get("recorded_at_utc"), assume_utc_for_naive=True)
        if ptp_timestamp is None:
            # Only trust client-provided turn timestamps when they are timezone-aware.
            ptp_timestamp = _parse_iso_datetime(turn.get("timestamp_utc"), assume_utc_for_naive=False)
        if ptp_timestamp is not None:
            break

    if not has_ptp:
        if record.get("final_outcome_code") == "ptp_set":
            has_ptp = True
        else:
            promise_to_pay = (record.get("last_call_state") or {}).get("promise_to_pay") or {}
            if isinstance(promise_to_pay, dict) and promise_to_pay.get("confirmed") is True:
                has_ptp = True

    if has_ptp and ptp_timestamp is None and record.get("final_outcome_code") == "ptp_set":
        ptp_timestamp = _parse_iso_datetime(record.get("updated_at_utc"), assume_utc_for_naive=True)

    return has_ptp, ptp_timestamp


def _build_daily_rows(
    daily: Dict[str, Dict[str, int]],
    *,
    trend_days: int,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    unknown_row: Optional[Dict[str, Any]] = None

    for date_key, counters in daily.items():
        row = {
            "date": date_key,
            "calls_total": counters["calls_total"],
            "ended_calls": counters["ended_calls"],
            "ptp_calls_ended": counters["ptp_calls_ended"],
            "ptp_success_rate_ended": (
                round(counters["ptp_calls_ended"] / counters["ended_calls"], 4)
                if counters["ended_calls"] > 0
                else None
            ),
        }
        if date_key == "unknown":
            unknown_row = row
        else:
            rows.append(row)

    rows.sort(key=lambda item: item["date"])
    if trend_days > 0:
        rows = rows[-trend_days:]
    if unknown_row is not None:
        rows.append(unknown_row)
    return rows


def build_metrics_summary(
    records: List[Dict[str, Any]],
    *,
    trend_days: int = 14,
) -> Dict[str, Any]:
    status_counts: Counter[str] = Counter()
    daily: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"calls_total": 0, "ended_calls": 0, "ptp_calls_ended": 0}
    )

    ptp_calls_total = 0
    ptp_calls_ended = 0
    time_to_ptp_seconds: List[float] = []

    for record in records:
        if not isinstance(record, dict):
            continue

        status = str(record.get("status", "unknown"))
        status_counts[status] += 1

        created_at = _parse_iso_datetime(record.get("created_at_utc"), assume_utc_for_naive=True)
        created_day = created_at.date().isoformat() if created_at else "unknown"
        daily[created_day]["calls_total"] += 1
        if status == "ended":
            daily[created_day]["ended_calls"] += 1

        has_ptp, ptp_timestamp = _extract_ptp_info(record)
        if not has_ptp:
            continue

        ptp_calls_total += 1

        if status == "ended":
            ptp_calls_ended += 1
            daily[created_day]["ptp_calls_ended"] += 1

            if created_at is not None and ptp_timestamp is not None:
                duration = (ptp_timestamp - created_at).total_seconds()
                if duration >= 0:
                    time_to_ptp_seconds.append(duration)

    calls_total = len(records)
    ended_calls = int(status_counts.get("ended", 0))
    active_calls = int(status_counts.get("active", 0))

    avg_seconds = (
        round(sum(time_to_ptp_seconds) / len(time_to_ptp_seconds), 2)
        if time_to_ptp_seconds
        else None
    )
    median_seconds = (
        round(float(median(time_to_ptp_seconds)), 2) if time_to_ptp_seconds else None
    )

    return {
        "generated_at_utc": _utc_now_iso(),
        "calls_total": calls_total,
        "active_calls": active_calls,
        "ended_calls": ended_calls,
        "status_counts": dict(status_counts),
        "ptp_calls_total": ptp_calls_total,
        "ptp_calls_ended": ptp_calls_ended,
        "ptp_success_rate_ended": (
            round(ptp_calls_ended / ended_calls, 4) if ended_calls > 0 else None
        ),
        "ptp_success_rate_all_calls": (
            round(ptp_calls_total / calls_total, 4) if calls_total > 0 else None
        ),
        "time_to_ptp_samples": len(time_to_ptp_seconds),
        "avg_time_to_ptp_seconds": avg_seconds,
        "median_time_to_ptp_seconds": median_seconds,
        "avg_time_to_ptp_minutes": (
            round(avg_seconds / 60.0, 2) if avg_seconds is not None else None
        ),
        "median_time_to_ptp_minutes": (
            round(median_seconds / 60.0, 2) if median_seconds is not None else None
        ),
        "daily": _build_daily_rows(daily, trend_days=trend_days),
    }


def build_job_metrics_summary(
    job_records: List[Dict[str, Any]],
    *,
    attempt_events: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    state_counts: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    blocked_policy_total = 0
    blocked_suppression_total = 0

    for row in job_records:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state", "unknown"))
        state_counts[state] += 1

        failure_reason = row.get("failure_reason")
        if isinstance(failure_reason, str) and failure_reason:
            error_counts[failure_reason] += 1
            if failure_reason.startswith("blocked_policy_"):
                blocked_policy_total += 1
            if failure_reason.startswith("blocked_suppression_"):
                blocked_suppression_total += 1

        attempts = row.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            continue

        last_attempt = attempts[-1] if isinstance(attempts[-1], dict) else {}
        outcome = last_attempt.get("outcome_code")
        if isinstance(outcome, str) and outcome:
            outcome_counts[outcome] += 1
            if outcome.startswith("blocked_policy_"):
                blocked_policy_total += 1
            if outcome.startswith("blocked_suppression_"):
                blocked_suppression_total += 1

        error = last_attempt.get("error_code")
        if isinstance(error, str) and error:
            error_counts[error] += 1
            if error.startswith("blocked_policy_"):
                blocked_policy_total += 1
            if error.startswith("blocked_suppression_"):
                blocked_suppression_total += 1

    decision_code_counts: Counter[str] = Counter()
    attempt_events_total = 0
    contact_attempts_total = 0

    for event in attempt_events or []:
        if not isinstance(event, dict):
            continue
        decision_code = event.get("decision_code")
        if isinstance(decision_code, str) and decision_code:
            decision_code_counts[decision_code] += 1
        attempt_events_total += 1
        if bool(event.get("counts_toward_attempt", False)):
            contact_attempts_total += 1

    return {
        "jobs_total": len(job_records),
        "state_counts": dict(state_counts),
        "outcome_counts": dict(outcome_counts),
        "error_counts": dict(error_counts),
        "blocked_policy_total": blocked_policy_total,
        "blocked_suppression_total": blocked_suppression_total,
        "attempt_events_total": attempt_events_total,
        "contact_attempts_total": contact_attempts_total,
        "decision_code_counts": dict(decision_code_counts),
    }

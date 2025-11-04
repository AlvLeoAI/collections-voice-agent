from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from src.api.contact_attempt_store import JsonContactAttemptStore
from src.api.outbound_orchestration import CallPolicySnapshot


def _parse_window(window: str) -> Tuple[int, int]:
    start_str, end_str = window.split("-", 1)
    start_h, start_m = [int(x) for x in start_str.strip().split(":")]
    end_h, end_m = [int(x) for x in end_str.strip().split(":")]
    return start_h * 60 + start_m, end_h * 60 + end_m


def _is_local_time_allowed(policy: CallPolicySnapshot, now_utc: datetime) -> bool:
    if not policy.allowed_local_time_ranges:
        return True

    try:
        local_now = now_utc.astimezone(ZoneInfo(policy.timezone))
    except Exception:
        local_now = now_utc.astimezone(timezone.utc)

    current_minutes = local_now.hour * 60 + local_now.minute

    for window in policy.allowed_local_time_ranges:
        try:
            start, end = _parse_window(window)
        except Exception:
            continue

        if start <= end:
            if start <= current_minutes <= end:
                return True
        else:
            if current_minutes >= start or current_minutes <= end:
                return True
    return False


@dataclass(frozen=True)
class PreDialDecision:
    allowed: bool
    reason_code: str
    retryable: bool
    attempts_today: int
    retry_after_seconds: Optional[int] = None
    min_gap_blocked_minutes_remaining: Optional[int] = None


def evaluate_pre_dial_gate(
    *,
    account_ref: str,
    policy: CallPolicySnapshot,
    suppression_flags: Dict[str, bool],
    attempt_store: JsonContactAttemptStore,
    now_utc: Optional[datetime] = None,
) -> PreDialDecision:
    now = now_utc or datetime.now(timezone.utc)

    # Non-retryable suppression controls.
    if suppression_flags.get("dnc", False):
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_suppression_dnc",
            retryable=False,
            attempts_today=0,
        )
    if suppression_flags.get("cease_contact", False):
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_suppression_cease_contact",
            retryable=False,
            attempts_today=0,
        )
    if suppression_flags.get("legal_hold", False):
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_suppression_legal_hold",
            retryable=False,
            attempts_today=0,
        )

    if not _is_local_time_allowed(policy, now):
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_policy_outside_call_window",
            retryable=True,
            attempts_today=0,
            retry_after_seconds=900,
        )

    try:
        tz = ZoneInfo(policy.timezone)
    except Exception:
        tz = timezone.utc
    local_day_iso = now.astimezone(tz).date().isoformat()
    attempts_today = attempt_store.count_attempts_for_local_day(
        account_ref=account_ref,
        timezone_name=policy.timezone,
        local_day_iso=local_day_iso,
    )
    if attempts_today >= policy.daily_attempt_cap:
        next_local_midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        retry_after = int(max(60, (next_local_midnight.astimezone(timezone.utc) - now).total_seconds()))
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_policy_daily_attempt_cap",
            retryable=True,
            attempts_today=attempts_today,
            retry_after_seconds=retry_after,
        )

    last_attempt_iso = attempt_store.get_last_counted_attempt_at_utc(account_ref=account_ref)
    if last_attempt_iso:
        last_dt = datetime.fromisoformat(last_attempt_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
        elapsed_minutes = (now - last_dt).total_seconds() / 60.0
        if elapsed_minutes < policy.min_gap_minutes:
            remaining = max(1, int(round(policy.min_gap_minutes - elapsed_minutes)))
            return PreDialDecision(
                allowed=False,
                reason_code="blocked_policy_min_gap",
                retryable=True,
                attempts_today=attempts_today,
                retry_after_seconds=remaining * 60,
                min_gap_blocked_minutes_remaining=remaining,
            )

    return PreDialDecision(
        allowed=True,
        reason_code="allowed",
        retryable=True,
        attempts_today=attempts_today,
    )

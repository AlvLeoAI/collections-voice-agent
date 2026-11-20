from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.api.contact_attempt_store import JsonContactAttemptStore
from src.api.outbound_orchestration import CallPolicySnapshot

_WINDOW_RE = re.compile(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})")


def _parse_window(window: str) -> tuple[int, int]:
    if not isinstance(window, str):
        raise ValueError("Call window must be a string.")
    match = _WINDOW_RE.fullmatch(window)
    if not match:
        raise ValueError(f"Malformed call window: {window!r}")
    start_h, start_m, end_h, end_m = (int(group) for group in match.groups())
    if not (0 <= start_h <= 23 and 0 <= start_m <= 59 and 0 <= end_h <= 23 and 0 <= end_m <= 59):
        raise ValueError(f"Out-of-range call window: {window!r}")
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    if start_minutes >= end_minutes:
        raise ValueError(f"Inverted or empty call window: {window!r}")
    return start_minutes, end_minutes


def _validate_windows(windows: list[str]) -> list[tuple[int, int]]:
    return [_parse_window(window) for window in windows]


def _validate_timezone(tz_name: str) -> ZoneInfo:
    if not isinstance(tz_name, str) or not tz_name.strip():
        raise ValueError("Timezone must be a non-empty IANA zone name.")
    return ZoneInfo(tz_name)


@dataclass(frozen=True)
class PreDialDecision:
    allowed: bool
    reason_code: str
    retryable: bool
    attempts_today: int
    retry_after_seconds: int | None = None
    min_gap_blocked_minutes_remaining: int | None = None


def evaluate_pre_dial_gate(
    *,
    account_ref: str,
    policy: CallPolicySnapshot,
    suppression_flags: dict[str, bool],
    attempt_store: JsonContactAttemptStore,
    now_utc: datetime | None = None,
) -> PreDialDecision:
    now = now_utc or datetime.now(UTC)

    # Merge account-level persisted suppressions with per-job flags via logical OR.
    persisted_suppressions = attempt_store.get_suppressions(account_ref)
    effective_dnc = bool(persisted_suppressions.get("dnc")) or bool(suppression_flags.get("dnc"))
    effective_cease_contact = bool(persisted_suppressions.get("cease_contact")) or bool(
        suppression_flags.get("cease_contact")
    )
    effective_legal_hold = bool(persisted_suppressions.get("legal_hold")) or bool(
        suppression_flags.get("legal_hold")
    )

    # Non-retryable suppression controls.
    if effective_dnc:
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_suppression_dnc",
            retryable=False,
            attempts_today=0,
        )
    if effective_cease_contact:
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_suppression_cease_contact",
            retryable=False,
            attempts_today=0,
        )
    if effective_legal_hold:
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_suppression_legal_hold",
            retryable=False,
            attempts_today=0,
        )

    # Fail-closed timezone validation.
    try:
        tz = _validate_timezone(policy.timezone)
    except Exception:
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_policy_invalid_timezone",
            retryable=False,
            attempts_today=0,
        )

    # Fail-closed policy parameter validation.
    if policy.daily_attempt_cap < 1 or policy.min_gap_minutes < 0:
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_policy_invalid_config",
            retryable=False,
            attempts_today=0,
        )

    # Fail-closed call window validation.
    if not policy.allowed_local_time_ranges:
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_policy_missing_call_window",
            retryable=False,
            attempts_today=0,
        )

    try:
        parsed_windows = _validate_windows(policy.allowed_local_time_ranges)
    except ValueError:
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_policy_invalid_call_window",
            retryable=False,
            attempts_today=0,
        )

    local_now = now.astimezone(tz)
    current_minutes = local_now.hour * 60 + local_now.minute
    if not any(start <= current_minutes <= end for start, end in parsed_windows):
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_policy_outside_call_window",
            retryable=True,
            attempts_today=0,
            retry_after_seconds=900,
        )

    local_day_iso = local_now.date().isoformat()
    attempts_today = attempt_store.count_attempts_for_local_day(
        account_ref=account_ref,
        timezone_name=policy.timezone,
        local_day_iso=local_day_iso,
    )
    if attempts_today >= policy.daily_attempt_cap:
        next_local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        retry_after = int(max(60, (next_local_midnight.astimezone(UTC) - now).total_seconds()))
        return PreDialDecision(
            allowed=False,
            reason_code="blocked_policy_daily_attempt_cap",
            retryable=True,
            attempts_today=attempts_today,
            retry_after_seconds=retry_after,
        )

    last_attempt_iso = attempt_store.get_last_counted_attempt_at_utc(account_ref=account_ref)
    if last_attempt_iso:
        last_dt = datetime.fromisoformat(last_attempt_iso.replace("Z", "+00:00")).astimezone(UTC)
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

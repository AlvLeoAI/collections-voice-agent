from __future__ import annotations

from typing import Any, Dict, Optional


def schedule_callback(
    *,
    datetime_local: str,
    timezone: str,
    reason: str,
) -> Dict[str, Any]:
    """
    Create a `schedule_callback` action for the host system.

    Purpose:
      Request the host to schedule a callback at a confirmed local datetime.

    Parameters:
      datetime_local:
        ISO-8601 local datetime string (confirmed with the user).
      timezone:
        IANA timezone string.
      reason:
        Non-sensitive reason for callback (e.g., "not_a_good_time").

    Returns:
      Action dict:
        {
          "datetime_local": <str>,
          "timezone": <str>,
          "reason": <str>
        }
    """
    raise NotImplementedError("Stub: host system must implement callback scheduling.")


def send_payment_link(
    *,
    channel: str,
    reference_id: str,
    expires_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Create a `send_payment_link` action for the host system.

    Purpose:
      Request the host to send a payment link after right-party verification and user agreement.
      Do not include raw payment credentials in any model inputs/outputs.

    Parameters:
      channel:
        Delivery channel identifier (e.g., "sms", "email") determined by the host.
      reference_id:
        Host-generated payment reference identifier (non-sensitive token).
      expires_minutes:
        Optional expiry time window for the link.

    Returns:
      Action dict:
        {
          "channel": <str>,
          "reference_id": <str>,
          "expires_minutes": <int|None>
        }
    """
    raise NotImplementedError("Stub: host system must implement payment-link delivery.")


def create_promise_to_pay(
    *,
    date: str,
    amount: str,
    currency: str,
    confirmed: bool,
) -> Dict[str, Any]:
    """
    Create a `create_promise_to_pay` action for the host system.

    Purpose:
      Record a confirmed promise-to-pay (PTP) after the user confirms amount and date.

    Parameters:
      date:
        Promise date in YYYY-MM-DD.
      amount:
        Decimal amount as a string (e.g., "50.00").
      currency:
        Currency code (e.g., "USD").
      confirmed:
        Whether the user explicitly confirmed the PTP details.

    Returns:
      Action dict:
        {
          "date": <str>,
          "amount": <str>,
          "currency": <str>,
          "confirmed": <bool>
        }
    """
    raise NotImplementedError("Stub: host system must implement PTP recording.")


def mark_do_not_contact(*, scope: str, reason: str) -> Dict[str, Any]:
    """
    Create a `mark_do_not_contact` action for the host system.

    Purpose:
      Record an explicit cease-contact request and prevent further contact per host policy.

    Parameters:
      scope:
        Host-defined scope identifier (e.g., "case", "number", "consumer").
      reason:
        Non-sensitive reason (e.g., "user_requested_cease_contact").

    Returns:
      Action dict:
        { "scope": <str>, "reason": <str> }
    """
    raise NotImplementedError("Stub: host system must implement DNC handling.")


def mark_wrong_number(*, reason: str) -> Dict[str, Any]:
    """
    Create a `mark_wrong_number` action for the host system.

    Purpose:
      Record that the contacted number does not reach the intended person (wrong party / wrong number).

    Parameters:
      reason:
        Non-sensitive reason string.

    Returns:
      Action dict:
        { "reason": <str> }
    """
    raise NotImplementedError("Stub: host system must implement wrong-number handling.")


def set_outcome(*, outcome_code: str, notes: Optional[str] = None) -> Dict[str, Any]:
    """
    Create a `set_outcome` action for the host system.

    Purpose:
      Persist a single outcome code for the call attempt.

    Parameters:
      outcome_code:
        Host-defined outcome code (e.g., "ptp_set", "callback_set", "wrong_party").
      notes:
        Optional non-sensitive notes.

    Returns:
      Action dict:
        { "outcome_code": <str>, "notes": <str|None> }
    """
    raise NotImplementedError("Stub: host system must implement outcome persistence.")


def end_call(*, reason: str) -> Dict[str, Any]:
    """
    Create an `end_call` action for the host system.

    Purpose:
      Request the host to end the call for a non-sensitive reason.

    Parameters:
      reason:
        Non-sensitive reason code (e.g., "silence_timeout", "callback_scheduled").

    Returns:
      Action dict:
        { "reason": <str> }
    """
    raise NotImplementedError("Stub: host system must implement call termination.")


def log_event(
    *,
    type: str,
    severity: str,
    message: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create a `log_event` action for the host system.

    Purpose:
      Log operational events without logging raw PII. Redact or omit sensitive values.

    Parameters:
      type:
        Event type identifier.
      severity:
        Severity string (e.g., "info", "warning", "error").
      message:
        Human-readable message (non-sensitive).
      data:
        Optional structured payload (must be non-sensitive / redacted).

    Returns:
      Action dict:
        {
          "type": <str>,
          "severity": <str>,
          "message": <str>,
          "data": <dict|None>
        }
    """
    raise NotImplementedError("Stub: host system must implement structured logging.")


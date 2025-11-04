from __future__ import annotations

from typing import Any, Dict

from ..state import CallState


def build_escalation_summary(*, call_state: CallState) -> str:
    """
    Build a short, non-sensitive escalation summary for a human agent.

    Purpose:
      Produce a 1–3 bullet summary per SKILL contract:
        - verification status (verified or not)
        - what was asked / what the user said (brief)
        - any proposed dates/amounts (post-verification only)

    Parameters:
      call_state:
        Current CallState. Do not include raw PII or sensitive account identifiers in the summary.

    Returns:
      A concise string suitable for the `escalate_to_human` action `summary` field.
    """
    raise NotImplementedError("Stub: host system must implement escalation summarization.")


def escalate_to_human(*, reason_code: str, summary: str) -> Dict[str, Any]:
    """
    Create an `escalate_to_human` action for the host system.

    Purpose:
      Request handoff to a human agent or specialized queue.

    Parameters:
      reason_code:
        One of the host/policy reason codes (e.g., dispute, legal, user_requested_human).
      summary:
        Non-sensitive summary string (see `build_escalation_summary`).

    Returns:
      Action dict:
        { "reason_code": <str>, "summary": <str> }
    """
    raise NotImplementedError("Stub: host system must implement human escalation.")


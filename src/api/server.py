import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Load environment variables
load_dotenv()

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT))

from src.api.call_store import JsonCallStore  # noqa: E402
from src.api.contact_attempt_store import JsonContactAttemptStore  # noqa: E402
from src.api.job_store import JsonJobStore  # noqa: E402
from src.api.metrics import build_job_metrics_summary, build_metrics_summary  # noqa: E402
from src.api.outbound_orchestration import (  # noqa: E402
    CallPolicySnapshot,
    JobState,
    OutboundCallPayload,
    RetryPolicy,
    TriggerSource,
)
from src.api.storage_utils import InvalidIdError  # noqa: E402
from src.outbound_voice_agent import CallState, handle_turn, start_call  # noqa: E402
from src.outbound_voice_agent.agent import TurnEvent  # noqa: E402

logger = logging.getLogger(__name__)

RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", "runtime"))

app = FastAPI(title="Outbound Agent API")
call_store = JsonCallStore(root_dir=RUNTIME_DIR / "calls")
job_store = JsonJobStore(root_dir=RUNTIME_DIR / "jobs")
attempt_store = JsonContactAttemptStore(root_dir=RUNTIME_DIR / "attempts")


class StartCallRequest(BaseModel):
    party_profile: dict[str, Any]
    # Required: an in-call cease-contact request must always have an account to be recorded against.
    account_ref: str = Field(min_length=1, max_length=128)


class TurnRequest(BaseModel):
    call_id: str
    turn_event: dict[str, Any]
    call_state: CallState | None = None
    party_profile: dict[str, Any]
    account_context: dict[str, Any]
    policy_config: dict[str, Any]
    account_ref: str | None = None


class EnqueueJobRequest(BaseModel):
    trigger_source: Literal["cron", "webhook", "manual"] = "manual"
    campaign_id: str
    account_ref: str
    party_profile: dict[str, str]
    account_context_ref: str
    language: str = "en-US"
    dnc: bool = False
    cease_contact: bool = False
    legal_hold: bool = False
    timezone: str = "America/Chicago"
    allowed_local_time_ranges: list[str] = Field(default_factory=lambda: ["08:00-20:00"])
    daily_attempt_cap: int = 2
    min_gap_minutes: int = 60
    scheduled_for_utc: str | None = None
    priority: int = 100
    max_attempts: int = 3
    base_delay_seconds: int = 120
    max_delay_seconds: int = 3600
    idempotency_key: str | None = None


class LeaseJobRequest(BaseModel):
    worker_id: str
    lease_seconds: int = 90


class CompleteJobRequest(BaseModel):
    outcome_code: str
    call_id: str | None = None


class FailJobRequest(BaseModel):
    error_code: str
    call_id: str | None = None


def _serialize_job(job: Any) -> dict[str, Any]:
    row = asdict(job)
    row["trigger_source"] = job.trigger_source.value
    row["state"] = job.state.value
    return row


@app.get("/health")
async def api_health():
    return {
        "status": "ok",
        "service": "collections-voice-agent",
        "llm_in_core_path": False,
    }


@app.post("/call/start")
async def api_start_call(request: StartCallRequest):
    try:
        call_id = call_store.generate_call_id()
        state = CallState()
        result = start_call(call_state=state, party_profile=request.party_profile)

        call_store.create_call(
            call_id=call_id,
            assistant_intent=result["assistant_intent"],
            call_state=result["call_state"],
            account_ref=request.account_ref,
        )

        return {
            "call_id": call_id,
            "assistant_text": result["assistant_text"],
            "assistant_intent": result["assistant_intent"],
            "actions": result.get("actions", []),
            "call_state": result["call_state"].model_dump(mode="json"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/call/turn")
async def api_handle_turn(request: TurnRequest):
    try:
        try:
            record = call_store.get_call(request.call_id)
            state = CallState.model_validate(record.get("last_call_state", {}))
        except InvalidIdError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

        try:
            event = TurnEvent(**request.turn_event)
        except TypeError as e:
            raise HTTPException(status_code=422, detail=f"Invalid turn_event payload: {e}") from e

        result = handle_turn(
            turn_event=event,
            call_state=state,
            party_profile=request.party_profile,
            account_context=request.account_context,
            policy_config=request.policy_config,
        )

        actions = result.get("actions", [])
        cease_requested = result["assistant_intent"] != "already_closed" and (
            result["call_state"].cease_contact_requested
            or any(action.get("action") == "mark_do_not_contact" for action in actions)
        )

        # Record the suppression BEFORE the ended call is persisted. If this write fails the
        # request fails with the call still open, so a retry replays the stop request instead
        # of hitting an already-closed call with the suppression lost.
        cease_contact_persisted: bool | None = None
        if cease_requested:
            # The account bound at /call/start wins; the request value only covers legacy records.
            account_ref = record.get("account_ref") or request.account_ref
            if account_ref:
                attempt_store.set_suppression(
                    account_ref=account_ref,
                    flag="cease_contact",
                    reason="in_call_stop_request",
                    call_id=request.call_id,
                )
                cease_contact_persisted = True
            else:
                cease_contact_persisted = False
                logger.warning(
                    "cease_contact requested but call has no account_ref; suppression NOT persisted call_id=%s",
                    request.call_id,
                )

        call_store.append_turn(
            call_id=request.call_id,
            turn_event=event,
            assistant_intent=result["assistant_intent"],
            actions=result["actions"],
            call_state=result["call_state"],
            nlu=result.get("nlu"),
        )

        return {
            "call_id": request.call_id,
            "assistant_text": result["assistant_text"],
            "assistant_intent": result["assistant_intent"],
            "actions": actions,
            "nlu": result.get("nlu"),
            "call_state": result["call_state"].model_dump(mode="json"),
            # True/False only on the turn that requested cease-contact; None otherwise.
            "cease_contact_persisted": cease_contact_persisted,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/call/{call_id}")
async def api_get_call_summary(call_id: str):
    try:
        return call_store.summarize_call(call_id)
    except HTTPException:
        raise
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/metrics/summary")
async def api_get_metrics_summary():
    try:
        call_metrics = build_metrics_summary(call_store.list_calls())
        jobs = [_serialize_job(job) for job in job_store.list_jobs()]
        attempts = attempt_store.list_recent_events(limit=5000)
        call_metrics["jobs"] = build_job_metrics_summary(jobs, attempt_events=attempts)
        return call_metrics
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/jobs/enqueue")
async def api_enqueue_job(request: EnqueueJobRequest):
    try:
        payload = OutboundCallPayload(
            account_ref=request.account_ref,
            party_profile=request.party_profile,
            account_context_ref=request.account_context_ref,
            language=request.language,
            suppression_flags={
                "dnc": request.dnc,
                "cease_contact": request.cease_contact,
                "legal_hold": request.legal_hold,
            },
        )
        policy = CallPolicySnapshot(
            timezone=request.timezone,
            allowed_local_time_ranges=request.allowed_local_time_ranges,
            daily_attempt_cap=request.daily_attempt_cap,
            min_gap_minutes=request.min_gap_minutes,
        )
        retry_policy = RetryPolicy(
            max_attempts=request.max_attempts,
            base_delay_seconds=request.base_delay_seconds,
            max_delay_seconds=request.max_delay_seconds,
        )

        job, created = job_store.enqueue_job(
            trigger_source=TriggerSource(request.trigger_source),
            campaign_id=request.campaign_id,
            payload=payload,
            policy=policy,
            scheduled_for_utc=request.scheduled_for_utc,
            priority=request.priority,
            retry_policy=retry_policy,
            idempotency_key=request.idempotency_key,
        )
        return {"created": created, "job": _serialize_job(job)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/jobs")
async def api_list_jobs(state: str | None = None, campaign_id: str | None = None, limit: int = 50):
    try:
        state_filter: JobState | None = None
        if state:
            try:
                state_filter = JobState(state)
            except ValueError as e:
                valid = ", ".join(s.value for s in JobState)
                raise HTTPException(
                    status_code=422, detail=f"Invalid state '{state}'. Valid values: {valid}"
                ) from e

        jobs = job_store.list_jobs(state=state_filter, campaign_id=campaign_id)
        if limit > 0:
            jobs = jobs[:limit]
        return {"count": len(jobs), "jobs": [_serialize_job(job) for job in jobs]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/jobs/{job_id}")
async def api_get_job(job_id: str):
    try:
        return _serialize_job(job_store.get_job(job_id))
    except HTTPException:
        raise
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/jobs/lease")
async def api_lease_job(request: LeaseJobRequest):
    try:
        job = job_store.lease_next_due_job(
            worker_id=request.worker_id,
            lease_seconds=request.lease_seconds,
        )
        return {"job": _serialize_job(job) if job else None}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/jobs/{job_id}/start")
async def api_start_job_attempt(job_id: str):
    try:
        return _serialize_job(job_store.mark_job_started(job_id))
    except HTTPException:
        raise
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/jobs/{job_id}/success")
async def api_complete_job(job_id: str, request: CompleteJobRequest):
    try:
        return _serialize_job(
            job_store.mark_job_succeeded(
                job_id,
                outcome_code=request.outcome_code,
                call_id=request.call_id,
            )
        )
    except HTTPException:
        raise
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/jobs/{job_id}/failure")
async def api_fail_job(job_id: str, request: FailJobRequest):
    try:
        return _serialize_job(
            job_store.mark_job_failed(
                job_id,
                error_code=request.error_code,
                call_id=request.call_id,
            )
        )
    except HTTPException:
        raise
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/attempts/{account_ref}")
async def api_get_attempts_for_account(account_ref: str):
    try:
        return {"account_ref": account_ref, "events": attempt_store.list_events(account_ref)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/attempts")
async def api_get_recent_attempts(limit: int = 200):
    try:
        return {"events": attempt_store.list_recent_events(limit=limit)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

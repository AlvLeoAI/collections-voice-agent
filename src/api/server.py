from dataclasses import asdict
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT))

from src.outbound_voice_agent import CallState, handle_turn, start_call
from src.outbound_voice_agent.agent import TurnEvent
from src.api.call_store import JsonCallStore
from src.api.contact_attempt_store import JsonContactAttemptStore
from src.api.job_store import JsonJobStore
from src.api.metrics import build_job_metrics_summary, build_metrics_summary
from src.api.outbound_orchestration import (
    CallPolicySnapshot,
    JobState,
    OutboundCallPayload,
    RetryPolicy,
    TriggerSource,
)

app = FastAPI(title="Outbound Agent API")
call_store = JsonCallStore()
job_store = JsonJobStore()
attempt_store = JsonContactAttemptStore()


class StartCallRequest(BaseModel):
    party_profile: Dict[str, Any]


class TurnRequest(BaseModel):
    call_id: str
    turn_event: Dict[str, Any]
    call_state: Optional[CallState] = None
    party_profile: Dict[str, Any]
    account_context: Dict[str, Any]
    policy_config: Dict[str, Any]


class EnqueueJobRequest(BaseModel):
    trigger_source: Literal["cron", "webhook", "manual"] = "manual"
    campaign_id: str
    account_ref: str
    party_profile: Dict[str, str]
    account_context_ref: str
    language: str = "en-US"
    dnc: bool = False
    cease_contact: bool = False
    legal_hold: bool = False
    timezone: str = "America/Chicago"
    allowed_local_time_ranges: List[str] = Field(default_factory=lambda: ["08:00-20:00"])
    daily_attempt_cap: int = 2
    min_gap_minutes: int = 60
    scheduled_for_utc: Optional[str] = None
    priority: int = 100
    max_attempts: int = 3
    base_delay_seconds: int = 120
    max_delay_seconds: int = 3600


class LeaseJobRequest(BaseModel):
    worker_id: str
    lease_seconds: int = 90


class CompleteJobRequest(BaseModel):
    outcome_code: str
    call_id: Optional[str] = None


class FailJobRequest(BaseModel):
    error_code: str
    call_id: Optional[str] = None


def _serialize_job(job: Any) -> Dict[str, Any]:
    row = asdict(job)
    row["trigger_source"] = job.trigger_source.value
    row["state"] = job.state.value
    return row


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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/call/turn")
async def api_handle_turn(request: TurnRequest):
    try:
        try:
            state = call_store.get_call_state(request.call_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

        try:
            event = TurnEvent(**request.turn_event)
        except TypeError as e:
            raise HTTPException(status_code=422, detail=f"Invalid turn_event payload: {e}")

        result = handle_turn(
            turn_event=event,
            call_state=state,
            party_profile=request.party_profile,
            account_context=request.account_context,
            policy_config=request.policy_config,
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
            "actions": result["actions"],
            "nlu": result.get("nlu"),
            "call_state": result["call_state"].model_dump(mode="json"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/call/{call_id}")
async def api_get_call_summary(call_id: str):
    try:
        return call_store.summarize_call(call_id)
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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
        )
        return {"created": created, "job": _serialize_job(job)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/jobs")
async def api_list_jobs(state: Optional[str] = None, campaign_id: Optional[str] = None, limit: int = 50):
    try:
        state_filter: Optional[JobState] = None
        if state:
            try:
                state_filter = JobState(state)
            except ValueError:
                valid = ", ".join(s.value for s in JobState)
                raise HTTPException(status_code=422, detail=f"Invalid state '{state}'. Valid values: {valid}")

        jobs = job_store.list_jobs(state=state_filter, campaign_id=campaign_id)
        if limit > 0:
            jobs = jobs[:limit]
        return {"count": len(jobs), "jobs": [_serialize_job(job) for job in jobs]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/jobs/{job_id}")
async def api_get_job(job_id: str):
    try:
        return _serialize_job(job_store.get_job(job_id))
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/jobs/{job_id}/start")
async def api_start_job_attempt(job_id: str):
    try:
        return _serialize_job(job_store.mark_job_started(job_id))
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/attempts/{account_ref}")
async def api_get_attempts_for_account(account_ref: str):
    try:
        return {"account_ref": account_ref, "events": attempt_store.list_events(account_ref)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/attempts")
async def api_get_recent_attempts(limit: int = 200):
    try:
        return {"events": attempt_store.list_recent_events(limit=limit)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

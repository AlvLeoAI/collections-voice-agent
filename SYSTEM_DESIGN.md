# System Design Document
## Outbound Collections Voice Agent Demo

**Date:** February 2026

## 1. Executive Summary

This project is a deterministic outbound collections voice-agent demo. It models call flow with explicit state transitions and emits host actions for outcomes such as promise-to-pay, escalation, and call ending.

Key properties:
- Compliance-first pre-verification behavior.
- Deterministic state updates across turns.
- Scenario replay support for repeatable demos.
- API + frontend sandbox for interactive testing.

## 2. Architecture Overview

### 2.1 Core Components

- `src/outbound_voice_agent/state.py`
  - Authoritative `CallState` dataclass and nested state objects.
- `src/outbound_voice_agent/agent.py`
  - Deterministic call logic via `start_call` and `handle_turn`.
- `src/outbound_voice_agent/tools/date_normalizer.py`
  - Date phrase normalization for negotiation flows.
- `scripts/run_outbound_demo.py`
  - Interactive and scenario replay CLI runner.
- `src/api/server.py`
  - FastAPI wrapper exposing `/call/start` and `/call/turn`.
- `frontend/app.py`
  - Streamlit sandbox consuming FastAPI endpoints.

### 2.2 Runtime Flow

1. Start call with `start_call` (pre-verification introduction).
2. For each turn, send `TurnEvent` to `handle_turn`.
3. Agent returns:
   - `assistant_text`
   - `assistant_intent`
   - `actions[]`
   - updated `call_state`
4. Host system (or demo runner) decides how to execute actions.

## 3. State Machine

Primary phases:
- `pre_verification`
- `verification`
- `post_verification`
- `closing`
- `escalation`
- `ended`

Key controls:
- Turn limits (`max_total_turns`)
- Silence counting and timeout
- Verification attempts
- Negotiation proposal caps

## 4. Compliance and Guardrails

- Pre-verification disclosure prohibition:
  - No debt/company/account details before right-party verification.
- Voice-first responses:
  - Short responses, one question per turn.
- Action payload discipline:
  - Keep action data non-sensitive and minimal.

## 5. Integration Boundary

The agent is integration-ready but demo-scoped:
- No telephony vendor integration.
- Call control tools are stubs for host implementation.
- Actions are contracts to be executed by an external orchestrator.

## 6. Known Gaps for Production

- Real telephony adapter and media pipeline.
- Durable persistence for call session state and outcomes.
- Action executor for side effects (callback scheduling, escalation routing, outcome logging).
- Full end-to-end monitoring and operational observability.

## 7. Demo Validation

Use JSON scenarios under `scripts/scenarios/` to validate deterministic behavior and edge cases.

## 8. Outbound Orchestration Blueprint

Recommended production-style trigger model:
- `cron` creates campaign jobs (wave dialing / scheduled callbacks).
- `webhook` creates event-driven jobs (new delinquency, broken PTP, inbound callback request).
- Both feed a single queue that workers consume.

Why this pattern:
- Centralized policy checks before dialing (timezone windows, retry caps, DNC, consent policies).
- Reliable retries with backoff and dead-letter handling.
- Strong idempotency guarantees to prevent duplicate calls.
- Horizontal scaling with controlled worker concurrency.

Reference contract:
- `src/api/outbound_orchestration.py`
  - job schema (`OutboundCallJob`)
  - trigger types (`TriggerSource`)
  - state machine (`JobState`, `JobEvent`, `STATE_TRANSITIONS`)
  - retry/backoff behavior
- `src/api/job_store.py`
  - durable JSON queue persistence (`runtime/jobs/*.json`)
- `src/api/contact_attempt_store.py`
  - per-account attempt/decision ledger (`runtime/attempts/*.json`)
- `scripts/run_outbound_worker.py`
  - queue consumer that initializes outbound call sessions (demo scope; no telephony dial)

### 8.1 Job State Machine

States:
- `queued` -> waiting for worker lease
- `leased` -> worker reserved job for a short TTL
- `running` -> active call execution
- `waiting_retry` -> retry deferred until `next_attempt_at_utc`
- terminal: `succeeded`, `dead_letter`, `canceled`

Allowed transitions (strict):
- `queued --lease--> leased`
- `leased --schedule_retry--> waiting_retry` (policy-gate defer before dial)
- `leased --start--> running`
- `running --call_succeeded--> succeeded`
- `running --call_failed--> failed`
- `failed --schedule_retry--> waiting_retry`
- `waiting_retry --retry_ready--> queued`
- `failed --exhaust_retries--> dead_letter`
- `queued|leased|running|waiting_retry --cancel--> canceled`

### 8.2 Compliance Gate Ordering

Before worker starts an outbound attempt:
1. Validate idempotency key has not already succeeded in the same campaign window.
2. Validate suppression controls (`dnc`, `cease_contact`, `legal_hold`).
3. Validate local-time call window and day-level attempt caps from attempt ledger.
4. Enforce min-gap minutes between counted attempts.
5. Start call session (`/call/start`) only after gate pass.
6. Enforce pre-verification disclosure prohibition until right-party verification is complete.

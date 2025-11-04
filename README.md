# Outbound Voice Agent (Collections Demo)

A portfolio/demo codebase for an outbound collections voice agent with deterministic call logic, scenario replay, API endpoints, and a Streamlit sandbox UI.

## What This Repository Contains

- Outbound agent state machine: `src/outbound_voice_agent/`
- Demo CLI runner (interactive + scenario replay): `scripts/run_outbound_demo.py`
- FastAPI backend: `src/api/server.py`
- Streamlit frontend sandbox: `frontend/app.py`
- Voice utility (TTS/STT helpers): `src/voice_handler.py`

## Quick Start

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Run scenario replay (no telephony)

```bash
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/happy_path_ptp_end_of_month.json
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/dispute_escalation.json
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/wrong_party.json
```

### 3. Run interactive CLI mode

```bash
python3 scripts/run_outbound_demo.py
```

### 4. Run API + frontend sandbox

```bash
python3 src/api/server.py
streamlit run frontend/app.py
```

Mic note:
- Direct browser recording in the frontend uses `st.audio_input` and requires Streamlit `>=1.39` (this repo pins `1.54.0`).
- If your browser blocks mic permission, Streamlit will not capture audio until permission is allowed.

### 5. Run one-command API smoke demo

No server needed in default mode (`inprocess`):

```bash
python3 scripts/smoke_api_demo.py --scenario happy_path
python3 scripts/smoke_api_demo.py --scenario dispute
python3 scripts/smoke_api_demo.py --scenario wrong_party
```

To test against a live FastAPI server instead:

```bash
python3 scripts/smoke_api_demo.py --mode http --base-url http://127.0.0.1:8000 --scenario happy_path
```

### 6. Run regression tests

```bash
python3 -m unittest tests/test_intent_classifier.py tests/test_outbound_agent_regressions.py tests/test_nlu_report.py -v
```

### 7. Analyze NLU confidence logs

```bash
python3 scripts/analyze_nlu_report.py --calls-dir runtime/calls --top 8
python3 scripts/analyze_nlu_report.py --calls-dir runtime/calls --json
```

### 8. Run outbound worker (queue consumer)

```bash
python3 scripts/run_outbound_worker.py --worker-id worker_local --once
```

Use continuous mode:

```bash
python3 scripts/run_outbound_worker.py --worker-id worker_local --poll-seconds 2 --max-jobs 25
```

### 9. One-command worker compliance smoke

```bash
python3 scripts/smoke_worker_compliance.py
```

## API Endpoints

- `POST /call/start`
  - Starts a new call and returns `call_id`, assistant response, and typed `call_state`.
- `POST /call/turn`
  - Accepts `call_id` + `turn_event` + context, advances state, returns updated `call_state`.
- `GET /call/{call_id}`
  - Returns a persisted call summary with turn/action history and final outcome fields.
- `GET /metrics/summary`
  - Returns demo metrics (success rate, time-to-PTP, daily trend) from persisted calls.
- `POST /jobs/enqueue`
  - Creates an outbound job (used by cron/webhook producers).
  - Supports suppression flags: `dnc`, `cease_contact`, `legal_hold`.
- `GET /jobs`
  - Lists jobs with optional state/campaign filters.
- `GET /jobs/{job_id}`
  - Fetches a single persisted job.
- `POST /jobs/lease`
  - Leases next due queued job for a worker.
- `POST /jobs/{job_id}/start`
  - Marks leased job as running and opens a new attempt.
- `POST /jobs/{job_id}/success`
  - Completes running job and persists outcome.
- `POST /jobs/{job_id}/failure`
  - Fails running job and schedules retry or dead-letter.
- `GET /attempts`
  - Returns recent pre-dial decision ledger entries.
- `GET /attempts/{account_ref}`
  - Returns attempt/decision history for one account reference.

## Persistence

- Call artifacts are stored locally under `runtime/calls/` as JSON files.
- Stored data is intentionally minimal and avoids raw user transcript logging.

## Outbound Trigger/Worker Design

For real outbound operations, use:
- Cron and webhooks to create call jobs.
- One queue/worker pipeline as the execution core.

Reference implementation contract:
- `src/api/outbound_orchestration.py` (job schema + worker state machine).
- `src/api/job_store.py` (durable JSON queue store under `runtime/jobs/`).
- `src/api/contact_attempt_store.py` (per-account attempt ledger under `runtime/attempts/`).
- `tests/test_outbound_orchestration.py` (transition + retry behavior).
- `tests/test_job_store.py` (enqueue/lease/retry persistence behavior).

## Notes

- This repository intentionally has no real telephony integration.
- Action outputs are demo dictionaries intended for a host system to consume.
- Before right-party verification, the agent must not disclose debt/account details.

# Outbound Collections Voice Agent

A fully functional outbound collections voice agent built as a deterministic state machine — no LLM required. Includes a REST API, Streamlit frontend with real-time TTS, scenario replay, an outbound job queue with compliance checks, and 44 unit/regression tests.

**Repository**: https://github.com/AlvLeoAI/collections-voice-agent

## Features

- **Deterministic state machine** — Phase-based agent (`pre_verification` → `verification` → `post_verification` → `ended`) with zero hallucination risk, sub-millisecond response decisions, and 100% reproducible behavior
- **Right-party verification gate** — Structural ZIP code match enforced in code (`agent.py:210`), not prompt instructions. No sensitive data leaks before verification passes
- **End-of-month payment policy** — Dates validated against the current month; out-of-range dates are rejected with reconduction to valid alternatives
- **Ambiguous date normalization** — Bilingual (ES/EN) date parser handles "mañana", "el viernes", "a fin de mes", "February 20", weekday names, and ISO dates with confidence scoring and confirmation flow
- **Intent classification** — Pattern-based classifier with 13 intent types, priority ranking, confidence scoring, and ambiguity detection for conflicting signals
- **Voice-first enforcement** — Every response hard-limited to 2 sentences and 1 question via `_enforce_voice_first()` guard
- **Silence and evasion handling** — Consecutive silence counter, low-confidence clarification with escalation, and distinct handling for confused/evasive/hostile tones
- **7 call-ending triggers** — PTP confirmed, goodbye, wrong party, silence timeout, max turns, verification failed, busy
- **4 escalation triggers** — Human handoff request, dispute, repeated refusal, low-confidence input
- **6 loop prevention counters** — Hard limits on verification attempts (3), reconduction (2), negotiation proposals (2), silence (3), clarification (1), and global turns (25)
- **FastAPI REST API** — Endpoints for call lifecycle (`/call/start`, `/call/turn`, `/call/{id}`), metrics (`/metrics/summary`), and outbound job management (`/jobs/*`)
- **Streamlit frontend** — Interactive sandbox with browser mic capture (`st.audio_input`), real-time call state visualization, and conversation history
- **ElevenLabs TTS** — `eleven_turbo_v2_5` model with streaming playback via `mpv` for low-latency voice output
- **Outbound job orchestration** — Queue-based worker with cron/webhook triggers, lease/retry/dead-letter lifecycle, exponential backoff, and compliance pre-dial checks (call windows, daily caps, suppression flags)
- **Pydantic state management** — `CallState` model with `extra="forbid"` for strict typed state, no undeclared fields
- **JSON persistence** — Zero-dependency file-based storage for calls, jobs, and attempt history under `runtime/`
- **NLU analytics** — Confidence reports, intent-vs-action confusion matrix, and outcome statistics across call history
- **44 unit/regression tests** — Covering intent classification, agent regressions, compliance, orchestration, stores, and metrics
- **5 scenario replays** — Happy path PTP, dispute escalation, wrong party, silence callback, date reconduction
- **No PII logging** — State contains only verification pass/fail flags, never raw user data

## Project Structure

```
src/
├── outbound_voice_agent/           # Core agent
│   ├── agent.py                    # State machine: start_call(), handle_turn()
│   ├── state.py                    # CallState (Pydantic, extra="forbid")
│   ├── intent_classifier.py        # Pattern-based intent classification
│   ├── prompts/                    # Prompt contracts (verifier, negotiation, fallback)
│   └── tools/                      # date_normalizer, call_control, escalation
├── api/                            # FastAPI backend
│   ├── server.py                   # REST endpoints
│   ├── call_store.py               # Call persistence
│   ├── compliance.py               # Pre-dial compliance checks
│   ├── outbound_orchestration.py   # Job state machine
│   ├── job_store.py                # Job queue with retry/dead-letter
│   ├── contact_attempt_store.py    # Per-account attempt ledger
│   └── metrics.py                  # Aggregated call/job metrics
├── voice_handler.py                # TTS/STT helpers (ElevenLabs streaming)
frontend/
└── app.py                          # Streamlit UI with mic capture + TTS
scripts/
├── run_outbound_demo.py            # CLI: interactive + scenario replay
├── smoke_api_demo.py               # API smoke tests (3 scenarios)
├── smoke_worker_compliance.py      # Compliance smoke tests
├── run_outbound_worker.py          # Queue consumer worker
├── analyze_nlu_report.py           # NLU confidence analytics
└── scenarios/                      # 5 predefined scenario JSON files
tests/                              # 44 unit/regression tests
runtime/                            # JSON persistence (calls/, jobs/, attempts/)
```

## Quick Start

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Run scenario replays (no server needed)

```bash
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/happy_path_ptp_end_of_month.json
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/dispute_escalation.json
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/wrong_party.json
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/silence_callback.json
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/date_too_late_reconduct.json
```

### 3. Run interactive CLI mode

```bash
python3 scripts/run_outbound_demo.py
```

### 4. Run API + frontend

```bash
# Terminal 1 — FastAPI (http://localhost:8000)
python3 src/api/server.py

# Terminal 2 — Streamlit (http://localhost:8501)
streamlit run frontend/app.py
```

Mic note: Browser recording uses `st.audio_input` and requires Streamlit `>=1.39` (this repo pins `1.54.0`). If your browser blocks mic permission, Streamlit will not capture audio until permission is allowed.

### 5. Run API smoke tests

No server needed in default mode (`inprocess`):

```bash
python3 scripts/smoke_api_demo.py --scenario happy_path
python3 scripts/smoke_api_demo.py --scenario dispute
python3 scripts/smoke_api_demo.py --scenario wrong_party
```

To test against a live FastAPI server:

```bash
python3 scripts/smoke_api_demo.py --mode http --base-url http://127.0.0.1:8000 --scenario happy_path
```

### 6. Run all tests

```bash
python3 -m unittest discover tests/ -v
```

### 7. Analyze NLU confidence logs

```bash
python3 scripts/analyze_nlu_report.py --calls-dir runtime/calls --top 8
python3 scripts/analyze_nlu_report.py --calls-dir runtime/calls --json
```

### 8. Run outbound worker

Single job:

```bash
python3 scripts/run_outbound_worker.py --worker-id worker_local --once
```

Continuous mode:

```bash
python3 scripts/run_outbound_worker.py --worker-id worker_local --poll-seconds 2 --max-jobs 25
```

### 9. Run compliance smoke tests

```bash
python3 scripts/smoke_worker_compliance.py
```

## API Endpoints

### Call Lifecycle

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/call/start` | Start a new call, returns `call_id`, assistant response, and typed `call_state` |
| `POST` | `/call/turn` | Advance call state with a turn event, returns updated response and actions |
| `GET` | `/call/{call_id}` | Retrieve persisted call summary with turn/action history and final outcome |
| `GET` | `/metrics/summary` | Aggregated metrics: success rate, time-to-PTP, daily trends, job stats |

### Outbound Job Queue

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/jobs/enqueue` | Create an outbound job (supports suppression flags: `dnc`, `cease_contact`, `legal_hold`) |
| `GET` | `/jobs` | List jobs with optional state/campaign filters |
| `GET` | `/jobs/{job_id}` | Fetch a single job |
| `POST` | `/jobs/lease` | Lease next due queued job for a worker |
| `POST` | `/jobs/{job_id}/start` | Mark leased job as running, open new attempt |
| `POST` | `/jobs/{job_id}/success` | Complete job, persist outcome |
| `POST` | `/jobs/{job_id}/failure` | Fail job, schedule retry or dead-letter |

### Contact Attempts

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/attempts` | Recent pre-dial decision ledger entries |
| `GET` | `/attempts/{account_ref}` | Attempt/decision history for one account |

## Persistence

- Call state and turn history persisted as JSON under `runtime/calls/`
- Job queue persisted under `runtime/jobs/` with full lifecycle tracking
- Contact attempt ledger under `runtime/attempts/` for compliance auditing
- No raw PII is logged — state contains only verification pass/fail flags

## Architecture

This agent uses a **fully deterministic approach with no LLM**. This is a deliberate choice for a compliance-critical domain:

- **Zero latency variance** — Deterministic code runs in single-digit milliseconds vs 200-2000ms for LLM inference
- **100% instruction adherence** — Code cannot hallucinate disclosures, skip verification, or fabricate payment terms
- **Fully testable** — Every code path is unit-testable with deterministic scenario replay
- **Zero per-call cost** — No API calls to inference providers, no vendor uptime dependency
- **Complete auditability** — Every decision traces to a specific line of code

See [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) for the full design decisions document.

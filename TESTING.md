# Testing Guide

All automated tests, linting, CLI scenarios, and smoke flows run **100% offline with zero API keys**.

## 1. Environment Sync, Lint & Unit/API Test Suite

```bash
uv sync --locked
uv lock --check
uv run ruff check .
uv run pytest -v
```

`tests/conftest.py` automatically clears all voice/AI API key environment variables and blocks outbound non-UNIX sockets during `pytest` runs.

## 2. Offline CLI Scenario Replays

```bash
uv run python scripts/run_outbound_demo.py --scenario scripts/scenarios/happy_path_ptp_end_of_month.json
uv run python scripts/run_outbound_demo.py --scenario scripts/scenarios/dispute_escalation.json
uv run python scripts/run_outbound_demo.py --scenario scripts/scenarios/wrong_party.json
uv run python scripts/run_outbound_demo.py --scenario scripts/scenarios/silence_callback.json
uv run python scripts/run_outbound_demo.py --scenario scripts/scenarios/date_too_late_reconduct.json
```

## 3. Interactive CLI Demo

Text-only offline mode (default):

```bash
uv run python scripts/run_outbound_demo.py
```

Optional ElevenLabs voice playback (requires `uv sync --locked --extra voice` and `ELEVENLABS_API_KEY`):

```bash
uv run python scripts/run_outbound_demo.py --voice
```

- Press Enter on a blank line to simulate silence.
- Type `quit` to exit.

## 4. In-Process API Smoke Flows (No Dirty `runtime/`)

```bash
uv run python scripts/smoke_api_demo.py --mode inprocess --temp-runtime --scenario happy_path
uv run python scripts/smoke_api_demo.py --mode inprocess --temp-runtime --scenario dispute
uv run python scripts/smoke_api_demo.py --mode inprocess --temp-runtime --scenario wrong_party
```

## 5. Worker Compliance Smoke Test

```bash
uv run python scripts/smoke_worker_compliance.py
```

Expected:
- `PASS worker compliance smoke`
- One allowed job initializes a call (`state=succeeded`, `outcome=call_initialized`).
- One DNC job is canceled before dialing (`state=canceled`, `failure_reason=blocked_suppression_dnc`).

## 6. Live API + Streamlit Sandbox Smoke Test

In terminal 1:

```bash
uv run uvicorn src.api.server:app --host 0.0.0.0 --port 8000 --workers 1
```

In terminal 2:

```bash
uv run streamlit run frontend/app.py
```

Verify:
- `curl -sf http://127.0.0.1:8000/health` returns `{"status":"ok","service":"collections-voice-agent","llm_in_core_path":false}`.
- Initial outbound greeting appears in Streamlit with zero debt disclosure.
- Text input advances turns and updates `call_state` in the sidebar (no API keys needed).
- When `GEMINI_API_KEY` and/or `ELEVENLABS_API_KEY` are configured, sidebar **Voice Controls** unlock in-memory STT and in-browser TTS playback.

## 7. Local Docker Smoke Test (Zero API Keys)

```bash
docker build -t collections-voice-agent:local .
docker run --rm --entrypoint whoami collections-voice-agent:local
docker run --rm -d --name cva-smoke -p 18080:8080 collections-voice-agent:local
sleep 2
curl -sf http://127.0.0.1:18080/health
curl -sf -X POST http://127.0.0.1:18080/call/start \
  -H "Content-Type: application/json" \
  -d '{"party_profile":{"target_name":"Alex Morgan"}}'
docker stop cva-smoke
```

## Expected Core Behaviors

- No debt, balance, or creditor disclosure before right-party ZIP verification succeeds.
- Wrong-party responses in `pre_verification` or `verification` close cleanly as `wrong_party` without disclosure.
- Dispute language triggers immediate human escalation (`escalated_dispute`).
- Past dates and post-month-end dates are rejected with reconduction to a valid current-month date.
- In-call `"stop calling me"` persists `cease_contact=True` in the account suppression ledger, blocking future outbound dials even if a subsequent job has `cease_contact=False`.
- Fail-closed pre-dial gate blocks invalid timezones, missing/malformed/inverted call windows, and invalid cap/gap configurations.

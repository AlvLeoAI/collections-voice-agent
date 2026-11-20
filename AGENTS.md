# Repository Agent Guide (AGENTS.md)

Canonical instructions for AI coding agents working in `collections-voice-agent` (`AlvLeoAI/collections-voice-agent`).

## Purpose & Core Architectural Invariant
- Portfolio demonstration of **engineering judgment on when *not* to use an LLM** in a regulated consumer-finance domain (outbound collections).
- **Zero LLMs in the core decision path:** The call state machine (`pre_verification -> verification -> post_verification -> ended`), right-party ZIP gate, scored English-only intent classification, payment-date bounds (`today <= ptp_date <= end_of_month`), `Decimal`-based partial offers, and fail-closed pre-dial compliance gate MUST remain **100% deterministic Python with zero AI/LLM SDK calls**.
- **Optional voice adapters only:** Google Gemini 3.5 Transcribe (`google-genai`, Interactions API `client.interactions.create(..., store=False)`) for STT and ElevenLabs (`eleven_turbo_v2_5`) for in-browser TTS are strictly lazy-loaded inside `src/voice_handler.py` under optional extra `[project.optional-dependencies] voice`. Never introduce OpenAI or eager AI client initialization.

## Architecture & Module Layout
- **Core deterministic agent (`src/outbound_voice_agent/`):**
  - `agent.py`: `start_call()`, `handle_turn()`, `_handle_verification()`, `_validate_ptp_date()`, `_build_partial_offer()`, `_enforce_voice_first()`.
  - `state.py`: `CallState` Pydantic model (`ConfigDict(extra="forbid", validate_assignment=True)`). `last_user_utterance` is retained as `""` for schema compatibility and must never store raw transcripts.
  - `intent_classifier.py`: English-only (`en-US`) scored regex intent classifier (13 labels, curly-apostrophe normalization, negation-aware patterns, ambiguity detection).
  - `tools/date_normalizer.py`: ES/EN relative & explicit date parser returning `needs_confirmation` for ambiguous inputs.
- **FastAPI backend & local JSON stores (`src/api/`):**
  - `server.py`: REST API (`/health`, `/call/*`, `/jobs/*`, `/attempts/*`, `/metrics/summary`), configured via `RUNTIME_DIR` (default `runtime`).
  - `storage_utils.py`: `validate_safe_id()` (`^[A-Za-z0-9_-]{1,64}$`) and `atomic_write_json()` (same-dir temp file + `flush` + `os.fsync` + `os.replace`).
  - `call_store.py`: Atomic call store with unconditional `_sanitize_call_state()` (`last_user_utterance = ""`) and `account_ref` persistence.
  - `compliance.py`: Fail-closed `evaluate_pre_dial_gate()` (invalid timezone, missing/malformed/inverted call window, invalid cap/gap -> `allowed=False, retryable=False`) + logical-OR merge with persisted account suppressions.
  - `contact_attempt_store.py`: Per-account hashed JSON ledger (`events` + `suppressions` for `dnc`, `cease_contact`, `legal_hold`).
  - `outbound_orchestration.py` & `job_store.py`: Formal job state machine (`queued`, `leased`, `running`, `waiting_retry`, `succeeded`, `failed`, `dead_letter`, `canceled`), `JobEvent.LEASE_EXPIRED` reclamation, deterministic UTC-day / explicit `idempotency_key` deduplication.
  - `metrics.py`: Aggregated call PTP metrics and job/compliance counts.
- **Optional voice & UI:**
  - `src/voice_handler.py`: Lazy `VoiceHandler` (`stt_enabled`, `tts_enabled`, `VoiceNotConfiguredError`, metadata-only logs).
  - `frontend/app.py`: Streamlit sandbox (`API_BASE_URL`, offline text mode by default, in-memory STT, in-browser `st.audio()` TTS).
- **CLI & smoke scripts (`scripts/`):**
  - `run_outbound_demo.py`: Offline interactive CLI + scenario replay (`--voice` strictly opt-in).
  - `smoke_api_demo.py`: In-process (`--temp-runtime`) or HTTP API smoke flows.
  - `smoke_worker_compliance.py` & `run_outbound_worker.py`: Outbound queue worker & compliance smoke check.
  - `analyze_nlu_report.py`: NLU confidence and intent-to-action matrix.
- **Packaging & CI:**
  - `pyproject.toml`, `uv.lock`, `.env.example`, `Dockerfile` (non-root `appuser`, `--workers 1`), `.dockerignore`, `DEPLOYMENT.md`, `.github/workflows/ci.yml`.

## Hard Constraints (Do Not Violate)
1. **No LLM calls in `src/outbound_voice_agent/` or `src/api/`.**
2. **No real telephony / SIP / PSTN integration.**
3. **Pre-verification disclosure prohibition:** Before `call_state.right_party_verified == True`, never mention or imply debt, amounts, creditor names, or account details.
4. **No raw PII logging or persistence:** Never write user transcripts to `CallState.last_user_utterance`, never log raw user transcripts or assistant text in `src/voice_handler.py`, and never persist user voice recordings to disk in `frontend/app.py`.
5. **Fail-closed compliance:** Never fall back to UTC on invalid timezones or allow dialing when call windows are empty/malformed.
6. **Voice-first output:** Keep every assistant turn naturally within **<= 2 short sentences** and **<= 1 question** without silently truncating active questions.
7. **Loop control & ended-call idempotency:** Respect all attempt/turn caps; once `call_state.phase == "ended"`, subsequent turns return `already_closed` with empty actions and no duplicate side effects.
8. **Single-instance persistence boundary:** Local JSON stores use in-process `threading.Lock`; keep `--workers 1` in `Dockerfile` and `--max-instances 1` for the Cloud Run API service.
9. **Offline-first tests:** `tests/conftest.py` clears all AI/voice env vars and blocks non-`AF_UNIX` outbound sockets. Every test must pass 100% offline with zero API keys.
10. **No secrets or live credentials:** Never read, print, or commit `.env` or real API keys.
11. **Confirm before recording a promise:** `_confirm_ptp()` may be reached directly only for an unambiguous full-balance date. A negated, hedged, partial-amount, or post-refusal proposal must go through `confirm_payment_date` and an explicit yes, and a reply naming a different date or amount is a counter-offer, never acceptance.
12. **Every call has an account:** `/call/start` requires `account_ref`, and an in-call cease-contact suppression is written before the ended call is persisted.

## Standard Commands (`uv`)
```bash
# Install locked core dependencies (or add --extra voice for optional Gemini/ElevenLabs)
uv sync --locked

# Lint & lockfile check
uv lock --check
uv run ruff check .

# Full offline test suite (zero API keys, outbound sockets blocked)
GEMINI_API_KEY="" GOOGLE_API_KEY="" ELEVENLABS_API_KEY="" uv run pytest -v

# Replay all deterministic CLI scenarios offline
for s in scripts/scenarios/*.json; do uv run python scripts/run_outbound_demo.py --scenario "$s"; done

# API & worker compliance smoke flows (zero writes to runtime/)
uv run python scripts/smoke_api_demo.py --mode inprocess --temp-runtime --scenario happy_path
uv run python scripts/smoke_api_demo.py --mode inprocess --temp-runtime --scenario dispute
uv run python scripts/smoke_api_demo.py --mode inprocess --temp-runtime --scenario wrong_party
uv run python scripts/smoke_worker_compliance.py

# Local dev servers
uv run uvicorn src.api.server:app --host 0.0.0.0 --port 8000 --workers 1
uv run streamlit run frontend/app.py
```
